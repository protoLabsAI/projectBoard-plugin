"""#425: max-mode must not turn a provider failure into a capability one.

Max-mode builds a card N ways at once and swallows each candidate's error. With every
candidate dead the drive saw only `NoChangesError("max-mode: all N candidates produced
no diff")` — a CAPABILITY verdict — and climbed a rung. When they all died because the
PROVIDER did (a spent quota, #362; a model it can't serve, #420) that says nothing about
the model, and the single-dispatch path already rotates within the rung on both. The
review of #421 reproduced it: rung `[codex, …]`, `max_mode_n: 2`, codex refusing its
model — the drive dispatched `codex, codex`, escalated, then `opus, opus`, escalated
again. No mark, no rotation.
"""

from __future__ import annotations

from project_board import worktree
import project_board.loop as loop_mod

from test_loop import _DEAD_MODEL, _SESSION_LIMIT, _rung_env

_MAX_LADDER = {"coders": {"smart": ["codex", "sonnet"], "reasoning": ["opus"]}, "max_mode_n": 2}


def _blocks(store):
    return [c for c in store.calls if c[0] == "flag_blocked"]


def _no_diffs(monkeypatch, loop):
    """Every candidate worktree comes back empty — the only way max-mode reaches its
    all-failed edge. (The judge is the selector's empty-check when no gate is set.)"""

    async def _judge(feature, base, worktrees):
        return None

    monkeypatch.setattr(loop, "_judge_candidates", _judge)


async def test_max_mode_rotates_past_a_refusing_provider_instead_of_climbing(monkeypatch):
    """The reproduction from the #421 review. Both candidates on codex are refused, so the
    card moves to the sibling at the SAME rung — ladder untouched — and codex is marked, so
    the next card starts on sonnet instead of paying to rediscover it."""
    seen: list[tuple[str, int]] = []  # (coder, climbs so far) per candidate dispatch

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append((coder, len(store.escalated)))
        if coder == "codex":
            raise worktree.WorktreeError(_DEAD_MODEL)
        return ""  # sonnet runs, and comes back empty — ends the drive

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-mm", "title": "t", "spec": "s"})

    assert seen[:4] == [("codex", 0), ("codex", 0), ("sonnet", 0), ("sonnet", 0)], seen
    assert loop_mod.provider_is_down("codex")


async def test_max_mode_quota_on_every_candidate_switches_provider_without_a_backoff(monkeypatch):
    """A spent quota is the other provider failure: every candidate rate-limited means the
    provider is out, not that the model failed. Switch to the sibling at once — no 60s
    backoff on the exhausted provider, and no climb."""
    seen: list[tuple[str, int]] = []
    slept: list[float] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append((coder, len(store.escalated)))
        if coder == "codex":
            raise worktree.WorktreeError(_SESSION_LIMIT)
        return ""

    async def _sleep(delay):
        slept.append(delay)

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    monkeypatch.setattr("project_board.loop.asyncio.sleep", _sleep)
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-mq", "title": "t", "spec": "s"})

    assert seen[:4] == [("codex", 0), ("codex", 0), ("sonnet", 0), ("sonnet", 0)], seen
    assert slept == [], "a quota with a sibling left must rotate, not back off"
    assert not loop_mod.provider_is_down("codex")  # a quota is not a refusal — never marked


async def test_max_mode_on_a_one_provider_board_blocks_a_dead_provider_as_infra(monkeypatch):
    """No sibling to rotate to: the card blocks the way a single dispatch does — under
    `dispatch-infra`, naming the fix — instead of as an unexplained `terminal` no-diff."""
    seen: list[str] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        raise worktree.WorktreeError(_DEAD_MODEL)

    loop, store = _rung_env(monkeypatch, _dispatch, cfg={"coder": "codex", "max_mode_n": 2})
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-m1", "title": "t", "spec": "s"})

    assert seen == ["codex", "codex"]  # one fan-out, then the block
    blocked = _blocks(store)
    assert len(blocked) == 1 and blocked[0][3] == "dispatch-infra", blocked
    assert blocked[0][2].startswith("provider unavailable")
    assert "does not exist or you do not have access" in blocked[0][2]  # the evidence rides along
    assert store.escalated == []


async def test_a_candidate_that_reached_the_model_keeps_it_a_capability_failure(monkeypatch):
    """One candidate was refused, the other RAN and produced nothing. The model had its
    shot and missed, so this is exactly the capability failure it always was: it climbs,
    and the provider — which served a candidate — is not marked."""
    seen: list[str] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        if coder == "codex" and kw.get("gen") == 1:
            raise worktree.WorktreeError(_DEAD_MODEL)
        return ""

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-mx", "title": "t", "spec": "s"})

    assert seen[:2] == ["codex", "codex"] and "opus" in seen, f"a capability failure must climb, got {seen}"
    assert "sonnet" not in seen
    assert not loop_mod.provider_is_down("codex")


async def test_candidates_that_failed_on_the_provider_differently_stay_a_capability_failure(monkeypatch):
    """One error can stand for all of them only when they agree. A refusal and a quota take
    different edges (mark and rotate vs. back off), so re-raising either would misstate
    what happened to the other; the pre-#425 capability path is kept for this mix."""
    seen: list[str] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        if coder == "codex":
            raise worktree.WorktreeError(_DEAD_MODEL if kw.get("gen") == 1 else _SESSION_LIMIT)
        return ""

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-md", "title": "t", "spec": "s"})

    assert "opus" in seen and "sonnet" not in seen, seen
    assert not loop_mod.provider_is_down("codex")


def test_provider_failure_category_is_the_drives_own_definition():
    """The predicate max-mode asks of each candidate is the one the drive rotates on: only
    a coder DISPATCH failure of a provider class counts — the same refusal words in a
    reviewer's gap, and every other dispatch failure, do not."""
    assert loop_mod.provider_failure_category(worktree.WorktreeError(_DEAD_MODEL)) == "provider_unavailable"
    assert loop_mod.provider_failure_category(worktree.WorktreeError(_SESSION_LIMIT)) == "rate_limit"
    gap = worktree.WorktreeError("goal verification failed: no test covers the model_not_found branch")
    assert loop_mod.provider_failure_category(gap) is None
    crash = worktree.WorktreeError("coder dispatch failed: adapter rejected the session (unknown error)")
    assert loop_mod.provider_failure_category(crash) is None
    assert loop_mod.provider_failure_category(worktree.CoderTimeout("coder timed out after 1800s")) is None
