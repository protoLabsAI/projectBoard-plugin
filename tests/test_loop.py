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


def test_build_prompt_requires_tests():
    """The coder's definition of done includes writing tests — the #897 lesson:
    a feature merged testless because nothing in the prompt or gate mandated it."""
    prompt = BoardLoop({})._build_prompt(FEATURE).lower()
    assert "automated tests" in prompt
    assert "definition of done" in prompt
    assert "rejected before the pr opens" in prompt


def test_is_test_path_classification():
    """The deterministic gate's path classifier — what counts as a test vs code."""
    from project_board.loop import _is_code_path, _is_test_path

    for p in ("tests/test_inbox.py", "test_x.py", "inbox/foo_test.py", "conftest.py", "web/x.test.tsx"):
        assert _is_test_path(p), p
    for p in ("inbox/store.py", "README.md", "config.yaml"):
        assert not _is_test_path(p), p
    assert _is_code_path("inbox/store.py") and _is_code_path("web/x.tsx")
    assert not _is_code_path("README.md") and not _is_code_path("config.yaml")


def test_format_cmd_parsed_from_config():
    assert BoardLoop({}).format_cmd == ""  # off by default
    assert BoardLoop({"format_cmd": "ruff check --fix ."}).format_cmd == "ruff check --fix ."


async def test_run_fixups_noop_when_unset(monkeypatch):
    """No format_cmd → _run_fixups must not shell out (it's the pre-PR auto-fix hook)."""
    loop = BoardLoop({})
    shelled = []

    async def _spy(*a, **k):
        shelled.append(1)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _spy)
    await loop._run_fixups("/wt")
    assert not shelled


# ── pre-PR local gate (bd-xbh) ───────────────────────────────────────────────────


def test_local_gate_config_parsed():
    assert BoardLoop({}).local_gate_cmd == ""  # off by default
    assert BoardLoop({}).local_gate_max == 2
    loop = BoardLoop({"local_gate_cmd": "ruff check .", "local_gate_max": 1})
    assert loop.local_gate_cmd == "ruff check ." and loop.local_gate_max == 1


async def test_run_local_gate_noop_when_unset(monkeypatch):
    """No local_gate_cmd → never shells out."""
    shelled = []

    async def _spy(*a, **k):
        shelled.append(1)

    monkeypatch.setattr("asyncio.create_subprocess_shell", _spy)
    assert await BoardLoop({})._run_local_gate("/wt") is None
    assert not shelled


async def test_run_local_gate_passes_and_captures_failure(tmp_path):
    """Exit 0 → None (pass); non-zero → captured output for the coder."""
    assert await BoardLoop({"local_gate_cmd": "exit 0"})._run_local_gate(str(tmp_path)) is None
    out = await BoardLoop({"local_gate_cmd": "echo boom 1>&2; exit 1"})._run_local_gate(str(tmp_path))
    assert out is not None and "boom" in out


async def test_run_local_gate_degrades_to_pass_on_launch_error(monkeypatch):
    """A gate that can't even spawn must not block — it degrades to pass (CI gates)."""

    async def _boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr("asyncio.create_subprocess_shell", _boom)
    assert await BoardLoop({"local_gate_cmd": "anything"})._run_local_gate("/wt") is None


# ── _drive: the state machine ───────────────────────────────────────────────────


async def _drive_with(monkeypatch, *, open_pr, coder=object(), dispatch=None, cfg=None, gate=None):
    """Run _drive over FEATURE with the worktree helpers + delegate stubbed.
    Returns the FakeLoopStore so the test can assert the recorded transitions."""
    store = FakeLoopStore()
    store.creates = []  # fids create_worktree was called for (a goal-fix retry reuses, so won't re-create)
    store.removes = []  # worktrees remove_worktree was called for
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root):
        store.creates.append(fid)
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _default_dispatch(c, wt, prompt, *, timeout=None):
        return "the coder's reply"

    async def _remove(repo, wt, branch=""):
        store.removes.append(wt)
        return None

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", dispatch or _default_dispatch)
    monkeypatch.setattr(worktree, "open_pr", open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)

    loop = BoardLoop(cfg or {"coder": "proto"})
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: coder)
    if gate is not None:
        monkeypatch.setattr(loop, "_run_local_gate", gate)
    await loop._drive(FEATURE)
    return loop, store


async def test_drive_opens_review_on_a_clean_build(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/1"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert loop._inflight == {}  # a completed drive leaves nothing to reap


async def test_drive_local_gate_failure_redispatches_then_opens(monkeypatch):
    """A pre-PR gate failure re-dispatches the SAME tier with the output injected,
    REUSING the worktree (one create), then opens the PR once the gate passes."""
    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None):
        prompts.append(prompt)
        return "reply"

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/1"

    gate_seq = iter(["FAILED tests/test_config.py::golden - boom", None])

    async def _gate(wt):
        return next(gate_seq)

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_dispatch,
        gate=_gate,
        cfg={"coder": "proto", "local_gate_cmd": "x", "local_gate_max": 2},
    )
    assert len(prompts) == 2  # initial + 1 gate-fix re-dispatch
    assert store.creates == ["bd-1"]  # keep-worktree → only one worktree created
    assert "boom" in prompts[1]  # the gate output was carried into the retry prompt
    assert ("open_review", "bd-1", "https://example/pr/1") in store.calls
    assert loop._gate_fix_attempts.get("bd-1", 0) == 0  # budget reset once the PR opened


async def test_drive_local_gate_exhausted_opens_pr_anyway(monkeypatch):
    """A persistent gate failure opens the PR after local_gate_max tries — never
    blocks (CI + the ci-fix budget are the backstop)."""
    prompts = []

    async def _dispatch(c, wt, prompt, *, timeout=None):
        prompts.append(prompt)
        return "reply"

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/2"

    async def _gate(wt):
        return "still red"

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_dispatch,
        gate=_gate,
        cfg={"coder": "proto", "local_gate_cmd": "x", "local_gate_max": 1},
    )
    assert len(prompts) == 2  # initial + 1 (local_gate_max) then opens anyway
    assert ("open_review", "bd-1", "https://example/pr/2") in store.calls
    assert not any(c[0] == "flag_blocked" for c in store.calls)  # never blocked


async def test_drive_blocks_on_an_empty_diff_with_a_single_coder(monkeypatch):
    async def _open_pr(wt, branch, *, base, title, body):
        raise worktree.NoChangesError("coder produced no commits")

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)
    # No escalation ladder (single coder) → a capability failure blocks immediately.
    assert "flag_blocked" in store.names()
    assert "open_review" not in store.names()
    assert loop._inflight == {}


# ── goal-verification gate (MiMo-borrowed; opt-in `goal_verify`) ─────────────────


async def test_goal_verify_pass_opens_the_pr(monkeypatch):
    async def _ok(self, feature, wt, base, coder_reply=""):
        return None  # PASS — no gap

    monkeypatch.setattr(BoardLoop, "_verify_goal", _ok)

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/9"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr, cfg={"coder": "proto", "goal_verify": True})
    assert ("open_review", "bd-1", "https://example/pr/9") in store.calls


async def test_goal_verify_gap_retries_same_tier_then_opens(monkeypatch):
    """A goal-verify gap (e.g. missing tests) re-dispatches the SAME coder with the
    gap carried into the prompt — and opens the PR once the coder fixes it."""
    calls = {"n": 0}

    async def _verify(self, feature, wt, base, coder_reply=""):
        calls["n"] += 1
        return "missing tests for the new behavior" if calls["n"] == 1 else None  # gap once, then PASS

    monkeypatch.setattr(BoardLoop, "_verify_goal", _verify)
    dispatched = []

    async def _disp(c, wt, prompt, *, timeout=None):
        dispatched.append(prompt)
        return "reply"

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/77"

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_disp,
        cfg={"coder": "proto", "goal_verify": True, "goal_fix_max": 2},
    )
    assert ("open_review", "bd-1", "https://example/pr/77") in store.calls  # opened after the retry
    assert len(dispatched) == 2  # initial + 1 same-tier re-dispatch
    # keep-worktree: the retry REUSES the worktree (impl intact) — created once, never removed
    assert store.creates == ["bd-1"]  # NOT re-created for the retry
    assert store.removes == []  # not wiped between attempts
    assert "ALREADY in this worktree" in dispatched[1] and "missing tests" in dispatched[1]  # add-to-existing feedback
    assert loop._goal_fix_attempts.get("bd-1") is None  # reset once the gate passes


async def test_goal_verify_gap_exhausts_retries_then_blocks(monkeypatch):
    """A persistent gap exhausts goal_fix_max same-tier retries, then blocks — no PR."""

    async def _gap(self, feature, wt, base, coder_reply=""):
        return "AC #1 unmet: multiply() missing"

    monkeypatch.setattr(BoardLoop, "_verify_goal", _gap)
    opened = []

    async def _open_pr(wt, branch, *, base, title, body):
        opened.append(True)
        return "https://example/pr/x"

    dispatched = []

    async def _disp(c, wt, prompt, *, timeout=None):
        dispatched.append(prompt)
        return "reply"

    loop, store = await _drive_with(
        monkeypatch,
        open_pr=_open_pr,
        dispatch=_disp,
        cfg={"coder": "proto", "goal_verify": True, "goal_fix_max": 2},
    )
    assert not opened  # the gate stopped the PR from being opened
    assert len(dispatched) == 3  # initial + goal_fix_max (2) same-tier retries
    assert store.creates == ["bd-1"]  # keep-worktree: created ONCE, reused across both retries
    assert "flag_blocked" in store.names()  # then blocked for triage
    assert "open_review" not in store.names()


async def test_goal_verify_off_by_default_skips_the_gate(monkeypatch):
    called = []

    async def _spy(self, feature, wt, base):
        called.append(True)
        return "would fail if invoked"

    monkeypatch.setattr(BoardLoop, "_verify_goal", _spy)

    async def _open_pr(wt, branch, *, base, title, body):
        return "https://example/pr/3"

    loop, store = await _drive_with(monkeypatch, open_pr=_open_pr)  # default cfg → off
    assert not called  # the gate is never invoked when goal_verify is off
    assert ("open_review", "bd-1", "https://example/pr/3") in store.calls


async def test_verify_goal_requires_a_test_deterministically(monkeypatch):
    """The gate is path-based — no LLM, no diff. A code change with no test file → gap;
    with a test → pass; docs/config-only → pass. Immune to diff truncation (the bug that
    made the old LLM verifier false-reject tests that sorted past the cap)."""
    loop = BoardLoop({"goal_verify": True})

    def _git_listing(names):
        async def _git(wt, *args, timeout=60):
            # `add -A` → empty; `diff --cached --name-only` → the changed-file list
            return (0, names if "--name-only" in args else "", "")

        return _git

    # code changed, NO test → gap
    monkeypatch.setattr(worktree, "_git", _git_listing("inbox/store.py\ngraph/config.py"))
    gap = await loop._verify_goal(FEATURE, "/wt", "main")
    assert gap and "no test" in gap.lower()

    # code changed WITH a test → pass (this is the case the old verifier wrongly blocked)
    monkeypatch.setattr(worktree, "_git", _git_listing("inbox/store.py\ntests/test_inbox.py"))
    assert await loop._verify_goal(FEATURE, "/wt", "main") is None

    # code changed, no test, but the coder declared NO_TEST_NEEDED → pass (escape hatch)
    monkeypatch.setattr(worktree, "_git", _git_listing("inbox/store.py"))
    reply = "Pure rename refactor.\nNO_TEST_NEEDED: behavior unchanged, covered by existing tests"
    assert await loop._verify_goal(FEATURE, "/wt", "main", reply) is None
    # ...but without the declaration, the same change is still a gap
    assert await loop._verify_goal(FEATURE, "/wt", "main", "I changed inbox/store.py") is not None

    # docs/config only → pass (no code change → no test required)
    monkeypatch.setattr(worktree, "_git", _git_listing("README.md\ndocs/x.md\nconfig.yaml"))
    assert await loop._verify_goal(FEATURE, "/wt", "main") is None

    # empty diff → None (open_pr's NoChangesError job, not the gate's)
    monkeypatch.setattr(worktree, "_git", _git_listing(""))
    assert await loop._verify_goal(FEATURE, "/wt", "main") is None


async def test_verify_goal_fails_open_when_no_criteria(monkeypatch):
    loop = BoardLoop({"goal_verify": True})
    # No acceptance_criteria → gate must not even shell out / call the model.
    assert await loop._verify_goal({"id": "x", "acceptance_criteria": ""}, "/wt", "main") is None


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

    async def _pr_ci(url, *, cwd=".", log_chars=3000):
        return ("passing", "")  # the OPEN PR's CI is green → left in review

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_ci_status", _pr_ci)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    await BoardLoop({})._reconcile_prs()
    assert store.merged == ["https://example/pr/1"]  # merged → done
    assert [b[0] for b in store.blocked] == ["bd-closed"]  # closed-unmerged → blocked
    assert set(reaped) == {"bd-merged", "bd-closed"}  # both terminal states reap; open kept


# ── the CI-feedback edge (closed-loop verify) ────────────────────────────────────


class _CiStore:
    def __init__(self, feature, escalate_tiers=None):
        self._feature = feature
        self.requeued = []
        self.blocked = []
        self.escalated = []
        self._escalate_tiers = list(escalate_tiers or [])

    def list_features(self, state=None):
        return [self._feature] if state == "in_review" else []

    def record_merge(self, *, pr_url):
        return None

    def requeue(self, fid):
        self.requeued.append(fid)
        return {"id": fid}

    def flag_blocked(self, fid, reason):
        self.blocked.append((fid, reason))

    def escalate(self, fid, reason):
        self.escalated.append((fid, reason))
        return self._escalate_tiers.pop(0) if self._escalate_tiers else None


async def _stub_ci_worktree(monkeypatch, *, ci, diff="- a\n+ b"):
    async def _pr_state(url, *, cwd="."):
        return "OPEN"

    async def _pr_ci(url, *, cwd=".", log_chars=3000):
        return ci() if callable(ci) else ci

    async def _pr_diff(url, *, cwd=".", max_chars=4000):
        return diff

    async def _reap(repo, root, fid):
        return None

    monkeypatch.setattr(worktree, "pr_state", _pr_state)
    monkeypatch.setattr(worktree, "pr_ci_status", _pr_ci)
    monkeypatch.setattr(worktree, "pr_diff", _pr_diff)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)


async def test_reconcile_ci_bounces_failing_pr_then_blocks(monkeypatch):
    """No coder ladder (single coder) → bounded same-tier retry capped by ci_fix_max."""
    store = _CiStore({"id": "bd-ci", "pr_url": "https://example/pr/9"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await _stub_ci_worktree(
        monkeypatch, ci=("failing", "Failing checks:\n- Web E2E: FAILURE\n\nFailing log:\nelement not found")
    )

    loop = BoardLoop({"ci_fix_max": 2})  # no `coders` → escalation_on is False
    assert not loop.escalation_on
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.requeued == ["bd-ci", "bd-ci"]
    assert store.blocked == [] and store.escalated == []
    assert "element not found" in loop._ci_feedback["bd-ci"]
    assert loop._ci_fix_attempts["bd-ci"] == 2
    # cap=2 exhausted → blocked, no further requeue.
    await loop._reconcile_prs()
    assert store.requeued == ["bd-ci", "bd-ci"]
    assert [b[0] for b in store.blocked] == ["bd-ci"]


async def test_reconcile_ci_escalates_through_tiers_then_blocks(monkeypatch):
    """With a coder ladder AND no same-tier budget (ci_fix_max=0), each CI failure
    climbs a tier (stronger model) carrying the prior diff; the top tier failing →
    Blocked (the ladder is the bound)."""
    store = _CiStore({"id": "bd-esc", "pr_url": "https://example/pr/7"}, escalate_tiers=["smart", "reasoning"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await _stub_ci_worktree(monkeypatch, ci=("failing", "Failing checks:\n- Tests: FAILURE"), diff="- old\n+ new")

    loop = BoardLoop({"coders": {"fast": "a", "smart": "b", "reasoning": "c"}, "ci_fix_max": 0})
    assert loop.escalation_on
    # CI failures climb tiers (escalate), requeue, NOT blocked, carrying the prior diff.
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.requeued == ["bd-esc", "bd-esc"]
    assert [e[0] for e in store.escalated] == ["bd-esc", "bd-esc"]
    assert store.blocked == []
    assert "- old" in loop._ci_prior_diff["bd-esc"]
    # top tier exhausted (escalate → None) → blocked.
    await loop._reconcile_prs()
    assert store.requeued == ["bd-esc", "bd-esc"]
    assert [b[0] for b in store.blocked] == ["bd-esc"]


async def test_reconcile_ci_spends_same_tier_budget_before_escalating(monkeypatch):
    """With a ladder AND ci_fix_max>0, a CI failure first spends same-tier fix
    attempts (cheap nits — lint, a golden-map update) before climbing a model tier,
    and the per-tier budget RESETS at the new rung. Without this, a one-line F841
    burned reasoning→opus and then blocked."""
    store = _CiStore({"id": "bd-b", "pr_url": "https://example/pr/5"}, escalate_tiers=["reasoning"])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    await _stub_ci_worktree(monkeypatch, ci=("failing", "Failing checks:\n- Lint: F841 unused variable"))

    loop = BoardLoop({"coders": {"smart": "a", "reasoning": "b"}, "ci_fix_max": 2})
    assert loop.escalation_on

    # First two failures: same-tier CI-fix (requeue), no escalation.
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.requeued == ["bd-b", "bd-b"]
    assert store.escalated == []
    assert loop._ci_fix_attempts["bd-b"] == 2

    # Budget exhausted → escalate ONE tier and reset the per-tier budget.
    await loop._reconcile_prs()
    assert [e[0] for e in store.escalated] == ["bd-b"]
    assert store.requeued == ["bd-b", "bd-b", "bd-b"]
    assert loop._ci_fix_attempts.get("bd-b", 0) == 0  # fresh budget at the new rung

    # The new rung gets its own same-tier attempts before the ladder is exhausted.
    await loop._reconcile_prs()
    await loop._reconcile_prs()
    assert store.requeued == ["bd-b", "bd-b", "bd-b", "bd-b", "bd-b"]
    assert [e[0] for e in store.escalated] == ["bd-b"]  # still just the one climb
    assert loop._ci_fix_attempts["bd-b"] == 2

    # Budget exhausted again → escalate returns None (ladder top) → blocked.
    await loop._reconcile_prs()
    assert [b[0] for b in store.blocked] == ["bd-b"]


async def test_reconcile_ci_leaves_passing_and_pending_in_review(monkeypatch):
    store = _CiStore({"id": "bd-ok", "pr_url": "https://example/pr/8"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    statuses = iter([("pending", ""), ("passing", "")])
    await _stub_ci_worktree(monkeypatch, ci=lambda: next(statuses))

    await BoardLoop({})._reconcile_prs()  # pending → leave
    await BoardLoop({})._reconcile_prs()  # passing → leave
    assert store.requeued == [] and store.blocked == []


def test_build_prompt_injects_ci_feedback_and_prior_diff():
    loop = BoardLoop({})
    feature = {"id": "bd-ci", "title": "T", "spec": "do it", "acceptance_criteria": "AC", "files_to_modify": ["a.py"]}
    assert "previous attempt was REJECTED" not in loop._build_prompt(feature)  # none stored → no block
    loop._ci_feedback["bd-ci"] = "Failing checks:\n- Web E2E: FAILURE\nelement not found"
    loop._ci_prior_diff["bd-ci"] = "--- a/x.tsx\n+++ b/x.tsx\n+ bad code"
    prompt = loop._build_prompt(feature)
    assert "previous attempt was REJECTED" in prompt
    assert "element not found" in prompt
    assert "bad code" in prompt  # the prior diff is carried forward


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


# ── max-mode best-of-N judge (#21) ───────────────────────────────────────────────


async def test_judge_candidates_returns_the_model_pick(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})

    async def _git(wt, *args, timeout=60):
        # distinct non-empty diff per worktree so every candidate competes
        return (0, f"diff for {wt}", "") if args[0] == "diff" else (0, "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _judge(prompt, *, system=None, model_name=None):
        assert "WHEN x THE SYSTEM SHALL y" in prompt  # acceptance criteria reach the judge
        return "Candidate 1 is the most complete."

    monkeypatch.setattr("graph.sdk.complete", _judge)
    assert await loop._judge_candidates(FEATURE, "main", ["/wt/a", "/wt/b"]) == 1


async def test_judge_candidates_none_when_all_empty(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})

    async def _git(wt, *args, timeout=60):
        return (0, "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _boom(*a, **k):
        raise AssertionError("judge must not run when there is nothing to judge")

    monkeypatch.setattr("graph.sdk.complete", _boom)
    assert await loop._judge_candidates(FEATURE, "main", ["/wt/a", "/wt/b"]) is None


async def test_judge_candidates_single_nonempty_skips_the_model(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})

    async def _git(wt, *args, timeout=60):
        if args[0] == "diff" and wt == "/wt/b":
            return (0, "real diff", "")
        return (0, "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _boom(*a, **k):
        raise AssertionError("judge must not run for a single candidate")

    monkeypatch.setattr("graph.sdk.complete", _boom)
    assert await loop._judge_candidates(FEATURE, "main", ["/wt/a", "/wt/b"]) == 1


async def test_judge_candidates_fails_open_to_first_when_judge_errors(monkeypatch):
    loop = BoardLoop({"max_mode_n": 2})

    async def _git(wt, *args, timeout=60):
        return (0, f"diff for {wt}", "") if args[0] == "diff" else (0, "", "")

    monkeypatch.setattr(worktree, "_git", _git)

    async def _err(prompt, *, system=None, model_name=None):
        raise RuntimeError("model offline")

    monkeypatch.setattr("graph.sdk.complete", _err)
    # both candidates non-empty → first non-empty index wins when the judge dies
    assert await loop._judge_candidates(FEATURE, "main", ["/wt/a", "/wt/b"]) == 0
