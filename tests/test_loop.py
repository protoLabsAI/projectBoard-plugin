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


async def _drive_with(monkeypatch, *, open_pr, coder=object()):
    """Run _drive over FEATURE with the worktree helpers + delegate stubbed.
    Returns the FakeLoopStore so the test can assert the recorded transitions."""
    store = FakeLoopStore()
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _create(repo, base, fid, root):
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _dispatch(c, wt, prompt):
        return "the coder's reply"

    async def _remove(repo, wt, branch=""):
        return None

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
    monkeypatch.setattr(worktree, "open_pr", open_pr)
    monkeypatch.setattr(worktree, "remove_worktree", _remove)

    loop = BoardLoop({"coder": "proto"})
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


# ── concurrency: _spawn_ready claims up to max_concurrent ────────────────────────


class _ClaimStore:
    """Yields a fixed sequence of features (then None) from claim_next_ready, and
    counts the claims so a test can prove the cap stops the puller."""

    def __init__(self, features):
        self._queue = list(features)
        self.claims = 0

    def claim_next_ready(self, assignee=""):
        self.claims += 1
        return self._queue.pop(0) if self._queue else None


async def test_spawn_ready_claims_up_to_max_concurrent(monkeypatch):
    store = _ClaimStore([{"id": "bd-1"}, {"id": "bd-2"}, {"id": "bd-3"}])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})

    release = asyncio.Event()

    async def _hold(feature):
        await release.wait()  # keep the drive task "running" so the slot stays taken

    monkeypatch.setattr(loop, "_drive", _hold)
    spawned = loop._spawn_ready()
    try:
        assert spawned is True
        assert len(loop._drives) == 2  # capped at max_concurrent
        assert store.claims == 2  # stopped claiming once full (no 3rd claim)
    finally:
        release.set()
        await asyncio.gather(*loop._drives, return_exceptions=True)


async def test_spawn_ready_is_false_when_nothing_ready(monkeypatch):
    store = _ClaimStore([])
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"max_concurrent": 2})
    assert loop._spawn_ready() is False
    assert loop._drives == set()


# ── the merge poll (Done-edge fallback) ─────────────────────────────────────────


class _PollStore:
    def __init__(self, in_review):
        self._in_review = in_review
        self.merged = []

    def list_features(self, state=None):
        return self._in_review if state == "in_review" else []

    def record_merge(self, *, pr_url):
        self.merged.append(pr_url)
        return {"id": "x", "board_state": "done"}


async def test_poll_merges_runs_done_edge_for_merged_only(monkeypatch):
    store = _PollStore(
        [
            {"id": "bd-1", "pr_url": "https://example/pr/1"},
            {"id": "bd-2", "pr_url": "https://example/pr/2"},
            {"id": "bd-3", "pr_url": ""},  # no PR → skipped entirely
        ]
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _is_merged(url, *, cwd="."):
        return url.endswith("/1")  # only PR 1 has merged

    reaped = []

    async def _reap(repo, root, fid):
        reaped.append(fid)

    monkeypatch.setattr(worktree, "pr_is_merged", _is_merged)
    monkeypatch.setattr(worktree, "reap_feature_worktree", _reap)

    await BoardLoop({})._poll_merges()
    assert store.merged == ["https://example/pr/1"]  # the unmerged + PR-less ones skipped
    assert reaped == ["bd-1"]  # the merged feature's worktree is reaped


async def test_maybe_poll_is_rate_limited(monkeypatch):
    loop = BoardLoop({"merge_poll": True, "merge_poll_interval_s": 60})
    calls = []

    async def _poll():
        calls.append(1)

    monkeypatch.setattr(loop, "_poll_merges", _poll)
    clock = {"t": 1000.0}
    monkeypatch.setattr("project_board.loop.time.monotonic", lambda: clock["t"])

    await loop._maybe_poll_merges()  # first → polls
    await loop._maybe_poll_merges()  # immediately again → rate-limited, skipped
    clock["t"] += 61
    await loop._maybe_poll_merges()  # interval elapsed → polls again
    assert len(calls) == 2


async def test_merge_poll_off_never_polls(monkeypatch):
    loop = BoardLoop({"merge_poll": False})
    called = []
    monkeypatch.setattr(loop, "_poll_merges", lambda: called.append(1))
    await loop._maybe_poll_merges()
    assert called == []  # disabled → the poll is never reached
