"""Loop tests — config parsing, the coder prompt, and the drive state machine.

``_drive`` is the only thing that moves a feature forward (``done`` is the merge
webhook's job). These tests stub the store (``loop.get_store``), the worktree
helpers (``worktree.create_worktree`` / ``dispatch_coder`` / ``open_pr`` /
``remove_worktree``), and the delegate lookup, then assert the transitions: a
clean build → ``open_review``; an empty diff with a single coder → ``flag_blocked``;
an unconfigured coder → ``flag_blocked`` before any worktree is created.
"""

from __future__ import annotations

import asyncio

from project_board import worktree
from project_board.loop import BoardLoop


class FakeLoopStore:
    def __init__(self):
        self.calls = []

    def current_tier(self, fid):
        return "fast"

    def open_review(self, fid, *, pr_url):
        self.calls.append(("open_review", fid, pr_url))
        return {"id": fid}

    def flag_blocked(self, fid, reason):
        self.calls.append(("flag_blocked", fid, reason))
        return {"id": fid}

    def names(self):
        return [c[0] for c in self.calls]


FEATURE = {
    "id": "bd-1",
    "title": "Add a thing",
    "repo": "/repo",
    "base_branch": "main",
    "spec": "do the thing",
    "design": "",
    "acceptance_criteria": "WHEN x THE SYSTEM SHALL y",
    "files_to_modify": ["a.py", "b.py"],
}


# ── config parsing ──────────────────────────────────────────────────────────────


def test_config_defaults():
    loop = BoardLoop({})
    assert loop.coder_name == "proto" and loop.reviewer_name == "quinn"
    assert loop.review_dispatch is False
    assert loop.interval == 30 and loop.enabled is False
    assert loop.escalation_on is False  # no coders map → single-coder mode
    assert loop.max_concurrent == 1  # serial by default
    assert loop.merge_poll is True and loop.merge_poll_interval == 60


def test_escalation_on_with_two_distinct_coders():
    loop = BoardLoop({"coders": {"fast": "proto", "smart": "proto-smart"}})
    assert loop.escalation_on is True


def test_max_concurrent_floors_at_one():
    assert BoardLoop({"max_concurrent": 0}).max_concurrent == 1
    assert BoardLoop({"max_concurrent": 4}).max_concurrent == 4


# ── the coder prompt (ProtoMaker discipline: name the files, demand the diff) ────


def test_build_prompt_is_imperative_and_lists_the_files():
    prompt = BoardLoop({})._build_prompt(FEATURE)
    assert "Add a thing" in prompt
    assert "do the thing" in prompt
    assert "- a.py" in prompt and "- b.py" in prompt
    assert "WHEN x THE SYSTEM SHALL y" in prompt
    assert "make all the edits here, now" in prompt.lower()


# ── _drive: the state machine ───────────────────────────────────────────────────


async def _drive_with(monkeypatch, *, open_pr, coder=object(), dispatch=None, cfg=None):
    """Run _drive over FEATURE with the worktree helpers + delegate stubbed.
    Returns the FakeLoopStore so the test can assert the recorded transitions."""
    store = FakeLoopStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _default_dispatch(c, wt, prompt, *, timeout=None):
        return "the coder's reply"

    async def _remove(repo, wt, branch=""):
        return None

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", dispatch or _default_dispatch)
    monkeypatch.setattr(worktree, "open_pr", open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)

    loop = BoardLoop(cfg or {"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: coder)
    await loop._drive(FEATURE)
    return loop, store


async def test_drive_opens_review_on_a_clean_build(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/1"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert loop._inflight == {}  # a completed drive leaves nothing to reap


async def test_drive_blocks_on_an_empty_diff_with_a_single_coder(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body):
        raise worktree.NoChangesError("coder produced no commits")

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    # No escalation ladder (single coder) → a capability failure blocks immediately.
    assert "flag_blocked" in store.names()
    assert "open_review" not in store.names()
    assert loop._inflight == {}


async def test_drive_blocks_when_the_coder_is_not_configured(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body):
        raise AssertionError("open_pr should not be reached")

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, coder=None)
    assert store.names() == ["flag_blocked"]  # blocked before any worktree work


# ── _drive: failure classification + backoff (no real sleeps) ───────────────────


async def _no_sleep(_delay):
    return None


async def test_drive_retries_a_transient_failure_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise worktree.WorktreeError("git push failed: connection reset by peer")
        return "https://example/pr/1"

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert calls["n"] == 2  # one transient retry, then success
    assert "flag_blocked" not in store.names()
    assert loop._inflight == {}


async def test_drive_blocks_after_exhausting_transient_retries(monkeypatch):
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body):
        calls["n"] += 1
        raise worktree.WorktreeError("gh pr create failed: 503 service unavailable")

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert "flag_blocked" in store.names()
    assert calls["n"] == 3  # transient policy = 3 attempts, then Blocked
    assert loop._inflight == {}


async def test_drive_blocks_immediately_on_a_terminal_failure(monkeypatch):
    calls = {"n": 0}

    async def _open_pr(wt, branch, *, base, title, body):
        calls["n"] += 1
        raise worktree.WorktreeError("gh pr create failed: 403 forbidden — bad credential")

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert "flag_blocked" in store.names()
    assert calls["n"] == 1  # auth is terminal → no retry


# ── _drive: the stuck-coder watchdog (CoderTimeout) ─────────────────────────────


async def test_drive_blocks_on_a_coder_timeout_not_transient_retried(monkeypatch):
    calls = {"n": 0}

    async def _dispatch(c, wt, prompt, *, timeout=None):
        calls["n"] += 1
        raise worktree.CoderTimeout("coder timed out after 1800s")

    async def _open_pr(wt, branch, *, base, title, body):
        raise AssertionError("open_pr should not run after a coder timeout")

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _no_sleep)
    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, dispatch=_dispatch)
    # A timeout matches "timed out" in classify (transient), but it's a CAPABILITY
    # failure → it must NOT be transient-retried: blocked after a single attempt.
    assert calls["n"] == 1
    assert "flag_blocked" in store.names()
    assert loop._inflight == {}


# ── concurrency: _spawn_ready claims up to max_concurrent ────────────────────────


class _ClaimStore:
    """A peekable ready queue + atomic claim(fid), mirroring the store API _spawn_ready
    now uses. Records claims so a test can prove the caps/gates stop the puller."""

    def __init__(self, features, in_review=0):
        self._features = [dict(f) for f in features]
        self._in_review = in_review
        self.claimed = []
        self.last_relaxed = None

    def ready_queue(self, relaxed=False):
        self.last_relaxed = relaxed
        return [f for f in self._features if f["id"] not in self.claimed]

    def claim(self, fid, assignee=""):
        if fid in self.claimed:
            return None
        self.claimed.append(fid)
        return next((f for f in self._features if f["id"] == fid), None)

    def list_features(self, state=None):
        return [{"id": f"rev-{i}"} for i in range(self._in_review)] if state == "in_review" else []


def _ready(fid, files):
    return {"id": fid, "board_state": "ready", "files_to_modify": files}


async def _hold_drives(loop, monkeypatch):
    """Replace _drive with a coroutine that blocks, so spawned tasks stay 'running'.
    Returns a finalizer the test calls to release + await them."""
    release = asyncio.Event()

    async def _hold(feature):
        await release.wait()

    monkeypatch.setattr(loop, "_drive", _hold)

    async def _finish():
        release.set()
        await asyncio.gather(*loop._drives, return_exceptions=True)

    return _finish


async def test_spawn_ready_claims_up_to_max_concurrent(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"]), _ready("bd-2", ["b.py"]), _ready("bd-3", ["c.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        assert loop._spawn_ready() is True
        assert len(loop._drives) == 2  # capped at max_concurrent
        assert store.claimed == ["bd-1", "bd-2"]  # stopped claiming once full
    finally:
        await finish()


async def test_spawn_ready_is_false_when_nothing_ready(monkeypatch):
    store = _ClaimStore([])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    assert loop._spawn_ready() is False
    assert loop._drives == set()


async def test_spawn_ready_skips_a_file_conflicting_candidate(monkeypatch):
    # bd-1 + bd-2 both touch shared.py; bd-3 touches other.py.
    store = _ClaimStore([_ready("bd-1", ["shared.py"]), _ready("bd-2", ["shared.py"]), _ready("bd-3", ["other.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 3})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        loop._spawn_ready()
        # bd-1 claimed; bd-2 deferred (overlaps bd-1's file); bd-3 claimed (disjoint).
        assert store.claimed == ["bd-1", "bd-3"]
        assert loop._inflight_files == {"bd-1": {"shared.py"}, "bd-3": {"other.py"}}
    finally:
        await finish()


async def test_spawn_ready_respects_the_review_wip_limit(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"])], in_review=5)  # already at the cap
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2, "max_pending_reviews": 5})
    assert loop._spawn_ready() is False
    assert store.claimed == []  # paused: too many PRs await review


async def test_drive_done_releases_its_files(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 1})

    async def _quick(feature):
        return None

    monkeypatch.setattr(loop, "_drive", _quick)
    loop._spawn_ready()
    await asyncio.gather(*list(loop._drives), return_exceptions=True)
    await asyncio.sleep(0)  # let the done-callbacks run
    assert loop._inflight_files == {}  # files released when the drive finished
    assert loop._drives == set()


# ── the PR reconcile (terminal-edge fallback) ───────────────────────────────────


class _ReconcileStore:
    def __init__(self, in_review):
        self._in_review = in_review
        self.merged = []
        self.blocked = []

    def list_features(self, state=None):
        return self._in_review if state == "in_review" else []

    def record_merge(self, *, pr_url):
        self.merged.append(pr_url)
        return {"id": "x", "board_state": "done"}

    def flag_blocked(self, fid, reason):
        self.blocked.append((fid, reason))


async def test_reconcile_drives_merged_to_done_and_closed_to_blocked(monkeypatch):
    store = _ReconcileStore(
        [
            {"id": "bd-merged", "pr_url": "https://example/pr/1"},
            {"id": "bd-closed", "pr_url": "https://example/pr/2"},
            {"id": "bd-open", "pr_url": "https://example/pr/3"},
            {"id": "bd-nopr", "pr_url": ""},  # no PR → skipped entirely
        ]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    states = {
        "https://example/pr/1": "MERGED",
        "https://example/pr/2": "CLOSED",
        "https://example/pr/3": "OPEN",
    }

    async def _pr_state(url, *, cwd="."):
        return states[url]

    reaped = []

    async def _reap(repo, root, fid):
        reaped.append(fid)

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    await BoardLoop({})._reconcile_prs()
    assert store.merged == ["https://example/pr/1"]  # merged → done
    assert [b[0] for b in store.blocked] == ["bd-closed"]  # closed-unmerged → blocked
    assert set(reaped) == {"bd-merged", "bd-closed"}  # both terminal states reap; open kept


async def test_maybe_reconcile_is_rate_limited(monkeypatch):
    loop = BoardLoop({"merge_poll": True, "merge_poll_interval_s": 60})
    calls = []

    async def _reconcile():
        calls.append(1)

    monkeypatch.setattr(loop, "_reconcile_prs", _reconcile)
    clock = {"t": 1000.0}
    monkeypatch.setattr("project_board.loop.time.monotonic", lambda: clock["t"])

    await loop._maybe_reconcile()  # first → reconciles
    await loop._maybe_reconcile()  # immediately → rate-limited
    clock["t"] += 61
    await loop._maybe_reconcile()  # interval elapsed → reconciles again
    assert len(calls) == 2


async def test_merge_poll_off_never_reconciles(monkeypatch):
    loop = BoardLoop({"merge_poll": False})
    called = []
    monkeypatch.setattr(loop, "_reconcile_prs", lambda: called.append(1))
    await loop._maybe_reconcile()
    assert called == []  # disabled → never reconciles


# ── crash recovery on boot ──────────────────────────────────────────────────────


class _RecoverStore:
    def __init__(self, in_progress):
        self._in_progress = in_progress
        self.calls = []

    def list_features(self, state=None):
        return self._in_progress if state == "in_progress" else []

    def open_review(self, fid, *, pr_url):
        self.calls.append(("open_review", fid, pr_url))

    def requeue(self, fid):
        self.calls.append(("requeue", fid))


async def test_recover_adopts_an_open_pr_else_resets_to_ready(monkeypatch):
    store = _RecoverStore([{"id": "bd-1"}, {"id": "bd-2"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _pr_url(branch, *, cwd="."):
        return "https://example/pr/1" if branch == "feat/bd-1" else ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _pr_url)
    await BoardLoop({})._recover()
    # bd-1 already had a PR (crash between open_pr and open_review) → adopt → in_review.
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    # bd-2 has no PR → reset to ready for a clean rebuild.
    assert ("requeue", "bd-2") in store.calls


async def test_recover_is_resilient_to_a_per_feature_error(monkeypatch):
    store = _RecoverStore([{"id": "bd-1"}, {"id": "bd-2"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _pr_url(branch, *, cwd="."):
        if branch == "feat/bd-1":
            raise RuntimeError("gh exploded")
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _pr_url)
    await BoardLoop({})._recover()  # must not raise
    # bd-1 errored and was skipped; bd-2 still recovered.
    assert ("requeue", "bd-2") in store.calls
    assert all(c[1] != "bd-1" for c in store.calls)


# ── periodic health sweep ───────────────────────────────────────────────────────


class _SweepStore:
    def __init__(self, in_progress=(), features=None):
        self._in_progress = list(in_progress)
        self._features = features or {}  # fid -> board_state
        self.requeued = []

    def list_features(self, state=None):
        return [{"id": f} for f in self._in_progress] if state == "in_progress" else []

    def requeue(self, fid):
        self.requeued.append(fid)

    def open_review(self, fid, *, pr_url):
        pass

    def get_feature(self, fid):
        st = self._features.get(fid)
        return {"id": fid, "board_state": st} if st else None


async def test_sweep_reconciles_in_progress_with_no_live_drive(monkeypatch):
    store = _SweepStore(in_progress=["bd-1", "bd-2"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: [])

    async def _no_pr(branch, *, cwd="."):
        return ""

    monkeypatch.setattr(worktree, "pr_url_for_branch", _no_pr)
    loop = BoardLoop({})
    loop._inflight_files = {"bd-2": {"a.py"}}  # bd-2 has a live drive → skip
    await loop._sweep()
    assert store.requeued == ["bd-1"]  # bd-1 (no PR, no drive) reset; bd-2 left alone


async def test_sweep_reaps_orphaned_worktrees(monkeypatch):
    store = _SweepStore(features={"bd-done": "done", "bd-rev": "in_review"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    monkeypatch.setattr(worktree, "list_feature_worktrees", lambda repo, root: ["bd-done", "bd-rev", "bd-gone"])
    reaped = []

    async def _reap(repo, root, fid):
        reaped.append(fid)

    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)
    await BoardLoop({})._sweep()
    # done + missing feature → reaped; in_review keeps its worktree (CI-fail re-dispatch).
    assert set(reaped) == {"bd-done", "bd-gone"}


async def test_maybe_sweep_is_rate_limited(monkeypatch):
    loop = BoardLoop({"health_sweep_interval_s": 300})
    calls = []

    async def _sweep():
        calls.append(1)

    monkeypatch.setattr(loop, "_sweep", _sweep)
    clock = {"t": 1000.0}
    monkeypatch.setattr("project_board.loop.time.monotonic", lambda: clock["t"])
    await loop._maybe_sweep()  # first → sweeps
    await loop._maybe_sweep()  # immediately → rate-limited
    clock["t"] += 301
    await loop._maybe_sweep()  # interval elapsed → sweeps again
    assert len(calls) == 2


async def test_sweep_off_when_interval_zero(monkeypatch):
    loop = BoardLoop({"health_sweep_interval_s": 0})
    called = []
    monkeypatch.setattr(loop, "_sweep", lambda: called.append(1))
    await loop._maybe_sweep()
    assert called == []  # disabled → never sweeps


# ── dependency gate (merge vs review) ───────────────────────────────────────────


def test_dep_gate_config_defaults_to_merge():
    assert BoardLoop({}).relaxed_gate is False
    assert BoardLoop({"dep_gate": "merge"}).relaxed_gate is False
    assert BoardLoop({"dep_gate": "review"}).relaxed_gate is True


async def test_spawn_ready_passes_the_dep_gate_to_ready_queue(monkeypatch):
    store = _ClaimStore([_ready("bd-1", ["a.py"])])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"dep_gate": "review", "max_concurrent": 1})
    finish = await _hold_drives(loop, monkeypatch)
    try:
        loop._spawn_ready()
        assert store.last_relaxed is True  # the relaxed gate reaches ready_queue
    finally:
        await finish()
