"""#425: max-mode must hand the drive what killed its candidates, not a "no diff".

Max-mode builds a card N ways at once and swallows each candidate's error. With every
candidate dead the drive saw only `NoChangesError("max-mode: all N candidates produced
no diff")` — a CAPABILITY verdict — and climbed a rung. When they all died because the
PROVIDER did (a spent quota, #362; a model it can't serve, #420) that says nothing about
the model, and the single-dispatch path already rotates within the rung on both. The
review of #421 reproduced it: rung `[codex, …]`, `max_mode_n: 2`, codex refusing its
model — the drive dispatched `codex, codex`, escalated, then `opus, opus`, escalated
again. No mark, no rotation. The same swallow hid a timeout from #378's counter and a
pre-model seam failure from #339's block.

When EVERY candidate raised, max-mode now re-raises the one error that speaks for them
(`representative_failure`, most specific edge first). Only a candidate that RETURNED —
ran, and came back with nothing — keeps the capability verdict.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from project_board import coder_seam, worktree
import project_board.loop as loop_mod

from test_loop import _DEAD_MODEL, _SESSION_LIMIT, _rung_env

_MAX_LADDER = {"coders": {"smart": ["codex", "sonnet"], "reasoning": ["opus"]}, "max_mode_n": 2}
_TIMEOUT = "coder timed out after 1800s"
_SEAM = "coder dispatch failed: adapter rejected the session (unknown error)"  # terminal
_RESET = "coder dispatch failed: connection reset by peer"  # transient → retryable


def _blocks(store):
    return [c for c in store.calls if c[0] == "flag_blocked"]


def _no_diffs(monkeypatch, loop):
    """Every candidate worktree comes back empty — the only way max-mode reaches its
    all-failed edge. (The judge is the selector's empty-check when no gate is set.)"""

    async def _judge(feature, base, worktrees):
        return None

    monkeypatch.setattr(loop, "_judge_candidates", _judge)


def _record_sleeps(monkeypatch) -> list[float]:
    slept: list[float] = []

    async def _sleep(delay):
        slept.append(delay)

    monkeypatch.setattr("project_board.loop.asyncio.sleep", _sleep)
    return slept


# ── precedence 0: a refused model ──────────────────────────────────────────────────────


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


async def test_a_refusal_beside_a_quota_takes_the_refusal_edge(monkeypatch):
    """One candidate refused, the other rate-limited: the provider is at least partly
    broken, and a capability climb is wrong either way. The refusal speaks — rotate and
    mark — rather than the quota's backoff on a provider that refuses its model."""
    seen: list[tuple[str, int]] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append((coder, len(store.escalated)))
        if coder == "codex":
            raise worktree.WorktreeError(_SESSION_LIMIT if kw.get("gen") == 1 else _DEAD_MODEL)
        return ""

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    slept = _record_sleeps(monkeypatch)
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-md", "title": "t", "spec": "s"})

    assert seen[:4] == [("codex", 0), ("codex", 0), ("sonnet", 0), ("sonnet", 0)], seen
    assert loop_mod.provider_is_down("codex") and slept == []


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


# ── precedence 1: a spent quota ────────────────────────────────────────────────────────


async def test_max_mode_quota_on_every_candidate_switches_provider_without_a_backoff(monkeypatch):
    """A spent quota: every candidate rate-limited means the provider is out, not that the
    model failed. Switch to the sibling at once — no 60s backoff on the exhausted provider,
    and no climb."""
    seen: list[tuple[str, int]] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append((coder, len(store.escalated)))
        if coder == "codex":
            raise worktree.WorktreeError(_SESSION_LIMIT)
        return ""

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    slept = _record_sleeps(monkeypatch)
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-mq", "title": "t", "spec": "s"})

    assert seen[:4] == [("codex", 0), ("codex", 0), ("sonnet", 0), ("sonnet", 0)], seen
    assert slept == [], "a quota with a sibling left must rotate, not back off"
    assert not loop_mod.provider_is_down("codex")  # a quota is not a refusal — never marked


async def test_a_quota_beside_a_timeout_takes_the_quota_edge(monkeypatch):
    """A rate limit outranks a timeout: the provider being out is the more specific news,
    and the timeout candidate may only have been starved by it. Rotate — and the timeout
    is not counted against the card's size."""
    seen: list[tuple[str, int]] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append((coder, len(store.escalated)))
        if coder == "codex":
            if kw.get("gen") == 1:
                raise worktree.CoderTimeout(_TIMEOUT)
            raise worktree.WorktreeError(_SESSION_LIMIT)
        return ""

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-mt", "title": "t", "spec": "s"})

    assert seen[:4] == [("codex", 0), ("codex", 0), ("sonnet", 0), ("sonnet", 0)], seen
    assert "bd-mt" not in loop._timeout_attempts


# ── precedence 2: a timeout ────────────────────────────────────────────────────────────


async def test_a_timeout_beside_a_seam_failure_reaches_the_timeout_handling(monkeypatch):
    """A timeout outranks a seam failure, so #378 sees it: the card's timeout count moves,
    and — the model having worked before the clock ran out — the climb carries #146's
    timeout context instead of a byte-identical prompt."""
    prompts: list[tuple[str, str]] = []

    async def _dispatch(coder, wt, prompt, *, fid=None, gen=1, **kw):
        prompts.append((coder, prompt))
        if coder == "codex" and gen == 1:
            coder_seam.progress_begin(fid, gen)
            coder_seam.progress_tool(fid, gen, {"phase": "start", "id": "t1", "name": "Edit"})
            raise worktree.CoderTimeout(_TIMEOUT)
        if coder == "codex":
            raise worktree.WorktreeError(_SEAM)
        return ""

    async def _no_commits(*_a, **_kw):
        raise worktree.NoChangesError("coder produced no commits vs base — nothing to PR")

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    monkeypatch.setattr(worktree, "open_pr", _no_commits)  # end the drive after the climb
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-mto", "title": "t", "spec": "s"})

    assert loop._timeout_attempts.get("bd-mto") == 1, "the timeout never reached #378's counter"
    assert [e[0] for e in store.escalated][:1] == ["bd-mto"]
    climbed = [p for c, p in prompts if c == "opus"]
    assert climbed and "TIMED OUT" in climbed[0]


# ── precedence 3 and 4: dispatch failures ──────────────────────────────────────────────


async def test_a_seam_failure_beside_a_retryable_one_is_blocked_as_pre_model(monkeypatch):
    """A dispatch failure the drive does not retry outranks one it would: with no model
    activity anywhere in the fan-out, #339 blocks it for triage — one fan-out, no backoff
    re-runs, no climb."""
    seen: list[str] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        raise worktree.WorktreeError(_RESET if kw.get("gen") == 1 else _SEAM)

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    slept = _record_sleeps(monkeypatch)
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-ms", "title": "t", "spec": "s"})

    assert seen == ["codex", "codex"] and slept == [] and store.escalated == []
    blocked = _blocks(store)
    assert len(blocked) == 1 and blocked[0][3] == "dispatch-infra", blocked
    assert "adapter rejected the session" in blocked[0][2]


async def test_a_retryable_failure_on_every_candidate_is_retried_not_climbed(monkeypatch):
    """Every candidate hit a network blip: the transient edge applies, as for a single
    dispatch — back off and re-run the fan-out, then block as `transient`, which the sweep
    heals. A blip is not a capability ceiling."""
    seen: list[str] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        raise worktree.WorktreeError(_RESET)

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    slept = _record_sleeps(monkeypatch)
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-mr", "title": "t", "spec": "s"})

    attempts = loop_mod.classify(_RESET).max_attempts
    assert set(seen) == {"codex"} and len(seen) == 2 * attempts, seen
    assert len(slept) == attempts - 1 and store.escalated == []
    blocked = _blocks(store)
    assert len(blocked) == 1 and blocked[0][3] == "transient"


# ── the fallback: a candidate RETURNED ─────────────────────────────────────────────────


async def test_a_candidate_that_returned_keeps_it_a_capability_failure(monkeypatch):
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


# ── the policy on its own ──────────────────────────────────────────────────────────────


def test_the_representative_is_the_most_specific_failure():
    refused = worktree.WorktreeError(_DEAD_MODEL)
    quota = worktree.WorktreeError(_SESSION_LIMIT)
    timeout = worktree.CoderTimeout(_TIMEOUT)
    seam = worktree.WorktreeError(_SEAM)
    blip = worktree.WorktreeError(_RESET)
    other = worktree.WorktreeError("worktree add failed: fatal: invalid reference")
    ladder = [refused, quota, timeout, seam, blip, other]
    for i, expected in enumerate(ladder):
        # each one beats everything after it, in any order
        rest = ladder[i:]
        assert loop_mod.representative_failure(list(reversed(rest))) is expected, expected
    # ties go to the earliest candidate
    first, second = worktree.WorktreeError(_RESET), worktree.WorktreeError(_RESET)
    assert loop_mod.representative_failure([first, second]) is first
    # only a WorktreeError can speak for the fan-out: a raw error never does
    raw = BrokenPipeError("[Errno 32] Broken pipe")
    assert loop_mod.representative_failure([raw, raw]) is None
    assert loop_mod.representative_failure([raw, blip]) is blip


def test_provider_failure_category_is_the_drives_own_definition():
    """The predicate max-mode ranks with is the one the drive rotates on: only a coder
    DISPATCH failure of a provider class counts — the same refusal words in a reviewer's
    gap, and every other dispatch failure, do not."""
    assert loop_mod.provider_failure_category(worktree.WorktreeError(_DEAD_MODEL)) == "provider_unavailable"
    assert loop_mod.provider_failure_category(worktree.WorktreeError(_SESSION_LIMIT)) == "rate_limit"
    gap = worktree.WorktreeError("goal verification failed: no test covers the model_not_found branch")
    assert loop_mod.provider_failure_category(gap) is None
    assert loop_mod.provider_failure_category(worktree.WorktreeError(_SEAM)) is None
    assert loop_mod.provider_failure_category(worktree.CoderTimeout(_TIMEOUT)) is None


# ── #425 review: the climb after a timeout, and what the fan-out leaves behind ───────


def _tool(fid, gen, name="Edit"):
    coder_seam.progress_begin(fid, gen)
    coder_seam.progress_tool(fid, gen, {"phase": "start", "id": f"t{gen}", "name": name})


async def test_a_climb_after_an_all_timeout_fan_out_still_fans_out(monkeypatch):
    """The timeout climb used to put its note in `_ci_feedback` — the slot that marks a
    carried-forward FIX, which switches fan-out off. So the climbed rung ran ONE opus
    dispatch instead of N. A timeout climb is a fresh build: the stronger rung fans out."""
    seen: list[tuple[str, int]] = []

    async def _dispatch(coder, wt, prompt, *, fid=None, gen=1, **kw):
        seen.append((coder, gen))
        if coder == "codex":
            _tool(fid, gen)
            raise worktree.CoderTimeout(_TIMEOUT)
        return ""

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-fo", "title": "t", "spec": "s"})

    assert seen == [("codex", 1), ("codex", 2), ("opus", 1), ("opus", 2)], seen


async def test_a_later_drive_of_the_card_fans_out_again_without_a_stale_note(monkeypatch):
    """`_ci_feedback` outlives the drive, so the note also switched fan-out off for every
    LATER drive of the card (a sweep requeue, an operator unblock) and opened each prompt
    with "a PREVIOUS attempt TIMED OUT". The note is the climbing drive's alone."""
    seen: list[tuple[str, int, bool]] = []

    async def _dispatch(coder, wt, prompt, *, fid=None, gen=1, **kw):
        seen.append((coder, gen, "TIMED OUT" in prompt))
        _tool(fid, gen)
        if coder == "opus":
            raise worktree.WorktreeError(_SEAM)  # the climbed rung dies another way
        raise worktree.CoderTimeout(_TIMEOUT)

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-rd", "title": "t", "spec": "s"})
    assert [s[2] for s in seen if s[0] == "opus"] == [True, True]  # the climb carried the note
    first = len(seen)
    await loop._drive({"id": "bd-rd", "title": "t", "spec": "s"})  # the requeue

    assert seen[first : first + 2] == [("codex", 1, False), ("codex", 2, False)], seen[first:]


async def test_the_timeout_note_is_spent_once_the_climbed_dispatch_has_run(monkeypatch):
    """The note is for the dispatch right after the climb. Once that dispatch has run, a
    retry of the same rung (here: its reply was empty) must not open with it again."""
    prompts: list[tuple[str, str]] = []

    async def _host_dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        prompts.append((coder, prompt))
        if coder == "a":
            coder_seam.progress_tool("bd-sp", 1, {"phase": "start", "id": "t1", "name": "Edit"})
            raise worktree.CoderTimeout(_TIMEOUT)
        return ""  # b runs and replies with nothing → the empty-reply same-tier retry

    async def _no_commits(*_a, **_kw):
        raise worktree.NoChangesError("coder produced no commits vs base — nothing to PR")

    async def _unused(*_a, **_kw):
        raise AssertionError("the real tap is under test")

    real_tapped = coder_seam.dispatch_coder_tapped
    loop, store = _rung_env(monkeypatch, _unused, cfg={"coders": {"smart": "a", "reasoning": "b"}}, tiers=["reasoning"])
    monkeypatch.setattr(coder_seam, "dispatch_coder_tapped", real_tapped)
    monkeypatch.setattr(worktree, "dispatch_coder", _host_dispatch)
    monkeypatch.setattr(worktree, "open_pr", _no_commits)
    await loop._drive({"id": "bd-sp", "title": "t", "spec": "s"})

    b = [p for c, p in prompts if c == "b"]
    assert len(b) >= 2, prompts
    assert "TIMED OUT" in b[0] and "TIMED OUT" not in b[1]


@dataclasses.dataclass
class _Coder:
    name: str
    workdir: str = ""


async def test_the_climb_note_describes_the_candidate_that_timed_out(monkeypatch):
    """Through the REAL tap. The note was mined from the LAST gen — here the candidate that
    failed fast on a seam error — so it told the stronger model "ran ~0.0s", no tool. The
    seam now stamps the gen its watchdog killed, and the note comes from that one."""
    real_tapped = coder_seam.dispatch_coder_tapped
    prompts: list[tuple[str, str]] = []

    async def _seam(coder, prompt, *, timeout=None, on_tool=None, on_thought=None, on_text=None):
        prompts.append((coder.name, prompt))
        if coder.name == "codex":
            if coder.workdir.endswith(".c0"):
                await on_tool({"phase": "start", "id": "t1", "name": "Edit", "input": {"path": "big_module.py"}})
                await on_thought("refactoring big_module.py section by section")
                await asyncio.Event().wait()  # hangs until the watchdog fires
            raise RuntimeError("adapter rejected the session (unknown error)")
        return "done"

    async def _unused(*_a, **_kw):
        raise AssertionError("the real tap is under test")

    loop, store = _rung_env(monkeypatch, _unused, cfg=dict(_MAX_LADDER, coder_timeout_s=0.2), tiers=["reasoning"])
    monkeypatch.setattr(coder_seam, "dispatch_coder_tapped", real_tapped)
    monkeypatch.setattr(coder_seam, "_import_dispatch_tapped", lambda: _seam)
    monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: _Coder(name))
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-tn", "title": "t", "spec": "s"})

    climbed = [p for c, p in prompts if c == "opus"]
    assert climbed and "TIMED OUT" in climbed[0]
    assert "Edit" in climbed[0] and "big_module.py" in climbed[0] and "refactoring big_module.py" in climbed[0]


async def test_every_candidate_is_reaped_before_the_drive_sees_the_failure(monkeypatch):
    """Re-raising must not leak a worktree: all N candidates are reaped, nothing promoted,
    BEFORE the drive handles the representative (here: blocks the one-provider board)."""

    async def _dispatch(coder, wt, prompt, **kw):
        raise worktree.WorktreeError(_DEAD_MODEL)

    loop, store = _rung_env(monkeypatch, _dispatch, cfg={"coder": "codex", "max_mode_n": 3})
    _no_diffs(monkeypatch, loop)
    created: list[str] = []

    async def _create(repo, base, fid, root, title="", **_kw):
        created.append(fid)
        return ("/wt/feat-" + fid, "feat/" + fid)

    async def _discard(repo, wt, branch=""):
        # The candidates are the drive's own, so they are discarded by path (#405).
        store.calls.append(("reap", wt[len("/wt/feat-") :]))
        return True

    async def _promote(*_a, **_kw):
        raise AssertionError("an all-raised fan-out promotes nothing")

    monkeypatch.setattr(worktree, "create_worktree", _create)
    monkeypatch.setattr(worktree, "remove_worktree", _discard)
    monkeypatch.setattr(worktree, "promote_worktree", _promote)
    await loop._drive({"id": "bd-rp", "title": "t", "spec": "s"})

    names = store.names()
    reaps = [c[1] for c in store.calls if c[0] == "reap"]
    assert sorted(reaps) == sorted(created) == ["bd-rp.c0", "bd-rp.c1", "bd-rp.c2"]
    assert max(i for i, n in enumerate(names) if n == "reap") < names.index("flag_blocked")
    assert loop._inflight == {}


async def test_a_cancelled_child_is_neither_returned_nor_raised(monkeypatch):
    """A CancelledError child did not fail: the fan-out keeps its old verdict rather than
    letting the other candidate's refusal speak for both."""

    async def _dispatch(coder, wt, prompt, **kw):
        if coder == "codex" and kw.get("gen") == 1:
            raise asyncio.CancelledError()
        if coder == "codex":
            raise worktree.WorktreeError(_DEAD_MODEL)
        return ""

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-cx", "title": "t", "spec": "s"})

    assert store.escalated and not loop_mod.provider_is_down("codex")


async def test_a_raw_error_on_every_candidate_keeps_the_shutdown_edge(monkeypatch):
    """An untapped host can surface a raw error (not a WorktreeError). Re-raised, it skipped
    the drive's shutdown and cancel checks and blocked the card as `unexpected` in the
    middle of a shutdown. It keeps the old no-diff verdict, which those checks see first."""

    async def _dispatch(coder, wt, prompt, **kw):
        loop._shutting_down = True
        raise BrokenPipeError("[Errno 32] Broken pipe")

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-sd", "title": "t", "spec": "s"})

    assert _blocks(store) == [], "a shutdown mid-fan-out flagged the card blocked"


async def test_a_candidate_that_returned_through_the_real_tap_keeps_it_a_capability_failure(monkeypatch):
    """The fallback, through the REAL `dispatch_coder_tapped` (the untapped host path)
    rather than a stand-in for it: one candidate refused, the other RAN and returned
    nothing. The card climbs as a capability failure — never a rotation to the sibling."""
    seen: list[str] = []

    async def _host_dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
        seen.append(coder)
        if coder == "codex" and wt.endswith(".c0"):
            raise worktree.WorktreeError(_DEAD_MODEL)
        return ""

    async def _unused(*_a, **_kw):
        raise AssertionError("the real tap is under test")

    real_tapped = coder_seam.dispatch_coder_tapped
    loop, store = _rung_env(monkeypatch, _unused, cfg=_MAX_LADDER, tiers=["reasoning"])
    monkeypatch.setattr(coder_seam, "dispatch_coder_tapped", real_tapped)
    monkeypatch.setattr(worktree, "dispatch_coder", _host_dispatch)
    _no_diffs(monkeypatch, loop)
    await loop._drive({"id": "bd-rt", "title": "t", "spec": "s"})

    assert "sonnet" not in seen, f"a fan-out where a candidate returned rotated instead of climbing: {seen}"
    assert store.escalated and "opus" in seen
    assert not loop_mod.provider_is_down("codex")


# ── #429 × #435: every candidate times out, twice ─────────────────────────────────────


@pytest.mark.skipif(
    not hasattr(loop_mod, "TOO_WIDE_CLASS"), reason="needs #435's too-wide park; runs once both are on the branch"
)
async def test_a_fan_out_that_keeps_timing_out_is_parked_for_its_split(monkeypatch):
    """Merged behaviour. Alone, #425's fix hands the drive a CoderTimeout, and a card whose
    fan-outs keep timing out blocks `transient` — the sweep then rebuilds it twice more, N
    candidates each time. With #435, the second fresh timeout parks it for a split: the
    first fan-out climbs (fanning out again at the new rung), the second parks."""
    seen: list[tuple[str, int]] = []

    async def _dispatch(coder, wt, prompt, *, fid=None, gen=1, **kw):
        seen.append((coder, gen))
        _tool(fid, gen)
        raise worktree.CoderTimeout(_TIMEOUT)

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_MAX_LADDER, tiers=["reasoning"])
    _no_diffs(monkeypatch, loop)
    asked: list[tuple[str, int]] = []
    budgets: dict[str, int] = {}

    def _ask(fid, *, timeouts):
        asked.append((fid, timeouts))
        return {"id": "bd-split", "board_state": "backlog"}

    store.request_decomposition = _ask
    store.record_budget = lambda fid, kind, n: budgets.__setitem__(f"{fid}:{kind}", n)
    store.get_feature = lambda fid: {"id": fid, "labels": [], "board_state": "in_progress"}
    await loop._drive({"id": "bd-tw", "title": "t", "spec": "s"})

    assert seen == [("codex", 1), ("codex", 2), ("opus", 1), ("opus", 2)], seen
    assert asked == [("bd-tw", 2)] and len(store.escalated) == 1
    assert _blocks(store)[-1][3] == loop_mod.TOO_WIDE_CLASS
