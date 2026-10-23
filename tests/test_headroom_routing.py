"""#579 (ADR 0122 D7/D8): headroom-aware sibling preference + threshold rotation.

The board steers which interchangeable delegate a card opens on by each one's live
subscription usage, read ONLY through ``graph.sdk.provider_usage`` (D8). The policy is a
pair of PURE reordering functions (``prefer_headroom_sibling`` / ``rotation_target``) so
it is tested on snapshot dicts, plus a drive-level check with a fake SDK.

The invariant the whole slice turns on (ADR 0122 D4): UNKNOWN == today. A rung on which
no sibling has a snapshot — the host lacks ``provider_usage``, or ``routing.headroom`` is
off — must give byte-for-byte the decisions the pre-#579 ``prefer_live_sibling`` /
``rotation_target`` gave. That is asserted directly against the live functions.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from project_board import worktree
import project_board.loop as loop_mod
from project_board.loop import BoardLoop
from project_board.routing import validate_routing

from test_loop import _rung_env


def _snap(headroom, *, usable=True, resets_at=None):
    """One ``graph.sdk.provider_usage`` snapshot: ``headroom`` percent of the binding
    window still free."""
    return {"usable": usable, "headroom_pct": headroom, "binding_window": "5h", "resets_at": resets_at}


# ── r1: prefer the higher-headroom sibling; round-robin when neither is spent ─────────


def test_a_nearly_spent_sibling_yields_to_the_one_with_headroom():
    """codex at 95% used, sonnet at 20%: a card on codex moves to sonnet (ADR 0122 D7)."""
    sibs = ["codex", "sonnet"]
    usage = {"codex": _snap(5), "sonnet": _snap(80)}
    assert loop_mod.prefer_headroom_sibling(sibs, 0, set(), usage, prefer_at=90) == 1
    # …and from sonnet's side the pick is already the healthy one — nothing to move.
    assert loop_mod.prefer_headroom_sibling(sibs, 1, set(), usage, prefer_at=90) == 1


def test_both_under_the_threshold_keeps_the_current_sibling():
    """With both below prefer_at% used the round-robin cursor is preserved — headroom only
    steers AWAY from a sibling that is actually running out."""
    sibs = ["codex", "sonnet"]
    usage = {"codex": _snap(50), "sonnet": _snap(80)}
    assert loop_mod.prefer_headroom_sibling(sibs, 0, set(), usage, prefer_at=90) == 0
    assert loop_mod.prefer_headroom_sibling(sibs, 1, set(), usage, prefer_at=90) == 1


def test_the_prefer_at_threshold_is_the_boundary():
    """At exactly prefer_at% used (headroom == 100 - prefer_at) the current sibling is
    preferred away from; one point under it is not."""
    sibs = ["codex", "sonnet"]
    assert (
        loop_mod.prefer_headroom_sibling(sibs, 0, set(), {"codex": _snap(10), "sonnet": _snap(70)}, prefer_at=90) == 1
    )
    assert (
        loop_mod.prefer_headroom_sibling(sibs, 0, set(), {"codex": _snap(11), "sonnet": _snap(70)}, prefer_at=90) == 0
    )


def test_the_most_headroom_wins_across_three_siblings():
    sibs = ["codex", "sonnet", "opus"]
    usage = {"codex": _snap(3), "sonnet": _snap(40), "opus": _snap(85)}
    assert loop_mod.prefer_headroom_sibling(sibs, 0, set(), usage, prefer_at=90) == 2


def test_a_sibling_that_already_failed_this_card_is_never_a_target():
    sibs = ["codex", "sonnet", "opus"]
    usage = {"codex": _snap(2), "sonnet": _snap(99), "opus": _snap(50)}
    # sonnet has the most headroom but already failed this card → opus is the pick.
    assert loop_mod.prefer_headroom_sibling(sibs, 0, {"sonnet"}, usage, prefer_at=90) == 2


# ── r2: usable:False with a future reset is down until resets_at ──────────────────────


def test_a_blocked_sibling_is_skipped_until_its_reset_then_eligible():
    loop_mod._PROVIDER_DOWN.clear()
    sibs = ["codex", "sonnet"]
    # codex is spent; sonnet has the headroom but is usable:False until t=2000.
    usage = {"codex": _snap(4), "sonnet": _snap(90, usable=False, resets_at=2000)}
    # before the reset, sonnet is skipped → the card stays on the exhausted current
    assert loop_mod.prefer_headroom_sibling(sibs, 0, set(), usage, prefer_at=90, now=1000) == 0
    # at/after the reset, sonnet is eligible again and wins on headroom
    assert loop_mod.prefer_headroom_sibling(sibs, 0, set(), usage, prefer_at=90, now=2000) == 1
    assert loop_mod.prefer_headroom_sibling(sibs, 0, set(), usage, prefer_at=90, now=2500) == 1


def test_a_usable_false_current_with_a_future_reset_moves_away():
    """The current sibling being down (not merely low) is itself a reason to move."""
    sibs = ["codex", "sonnet"]
    usage = {"codex": _snap(99, usable=False, resets_at=2000), "sonnet": _snap(40)}
    assert loop_mod.prefer_headroom_sibling(sibs, 0, set(), usage, prefer_at=90, now=1000) == 1


# ── r3: unknown == today (byte-identical to prefer_live_sibling / rotation_target) ────

# The exact cases the pre-#579 functions are pinned on (tests/test_loop.py).
_SIBLING_CASES = [
    (["codex", "sonnet"], 0, set()),
    (["codex", "sonnet"], 1, set()),
    (["codex", "sonnet"], 0, {"sonnet"}),
    (["codex"], 0, set()),
    (["codex", "sonnet", "opus"], 1, {"codex"}),
]
_ROTATION_CASES = [
    ("rate_limit", ["codex", "sonnet"], 0, {"codex"}),
    ("rate_limit", ["codex", "sonnet"], 1, {"sonnet"}),
    ("provider_unavailable", ["codex", "sonnet"], 1, {"codex", "sonnet"}),
    ("provider_unavailable", ["sonnet"], 0, {"sonnet"}),
    ("transient", ["codex", "sonnet"], 0, {"codex"}),
    ("rate_limit", ["codex", "sonnet", "opus"], 0, {"codex"}),
]


@pytest.mark.parametrize("sibs,cur,spent", _SIBLING_CASES)
def test_all_unknown_matches_prefer_live_sibling_exactly(sibs, cur, spent):
    loop_mod._PROVIDER_DOWN.clear()
    now = 1000.0
    loop_mod.mark_provider_down("codex", now=now)  # a down-mark the headroom path must honour too
    unknown = {name: None for name in sibs}
    assert loop_mod.prefer_headroom_sibling(sibs, cur, set(spent), unknown, now=now) == loop_mod.prefer_live_sibling(
        sibs, cur, set(spent), now=now
    )
    # an empty / absent usage map is unknown too
    assert loop_mod.prefer_headroom_sibling(sibs, cur, set(spent), {}, now=now) == loop_mod.prefer_live_sibling(
        sibs, cur, set(spent), now=now
    )


@pytest.mark.parametrize("cat,sibs,cur,spent", _ROTATION_CASES)
def test_all_unknown_rotation_matches_today(cat, sibs, cur, spent):
    loop_mod._PROVIDER_DOWN.clear()
    unknown = {name: None for name in sibs}
    assert loop_mod.rotation_target(cat, sibs, cur, set(spent), usage=unknown) == loop_mod.rotation_target(
        cat, sibs, cur, set(spent)
    )
    assert loop_mod.rotation_target(cat, sibs, cur, set(spent), usage=None) == loop_mod.rotation_target(
        cat, sibs, cur, set(spent)
    )


# ── rotation_target headroom preference ───────────────────────────────────────────────


def test_a_provider_failure_rotates_to_the_highest_headroom_sibling():
    loop_mod._PROVIDER_DOWN.clear()
    sibs = ["codex", "sonnet", "opus"]
    usage = {"codex": _snap(0), "sonnet": _snap(20), "opus": _snap(80)}
    # without usage the next sibling in order (sonnet) is chosen; with it, opus wins
    assert loop_mod.rotation_target("rate_limit", sibs, 0, {"codex"}) == 1
    assert loop_mod.rotation_target("rate_limit", sibs, 0, {"codex"}, usage=usage) == 2


def test_rotation_skips_a_blocked_sibling():
    loop_mod._PROVIDER_DOWN.clear()
    sibs = ["codex", "sonnet", "opus"]
    usage = {"codex": _snap(0), "sonnet": _snap(99, usable=False, resets_at=2000), "opus": _snap(30)}
    # sonnet is blocked until its reset → opus takes the rotation despite less headroom
    assert loop_mod.rotation_target("rate_limit", sibs, 0, {"codex"}, usage=usage, now=1000) == 2
    # after the reset, sonnet's headroom wins
    assert loop_mod.rotation_target("rate_limit", sibs, 0, {"codex"}, usage=usage, now=2000) == 1


# ── drive-level: a fake SDK, exclusive confinement, and the live toggle ───────────────

_LADDER = {"coders": {"standard": ["codex", "sonnet"], "reasoning": ["opus"]}}


def _fake_sdk(monkeypatch, usage):
    """Install a fake ``graph.sdk.provider_usage`` and record which delegates it was asked
    about (so a test can prove an excluded sibling was never even queried)."""
    queried: list[str] = []

    def _provider_usage(delegate):
        queried.append(delegate)
        return usage.get(delegate)

    monkeypatch.setattr("graph.sdk.provider_usage", _provider_usage, raising=False)
    return queried


async def test_drive_opens_on_the_higher_headroom_sibling(monkeypatch):
    """End to end: codex nearly spent, sonnet with headroom — the card opens on sonnet even
    though the round-robin cursor points at codex."""
    loop_mod._PROVIDER_DOWN.clear()
    seen: list[str] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        raise worktree.NoChangesError("coder produced no commits")

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_LADDER, cursor=0)
    _fake_sdk(monkeypatch, {"codex": _snap(4), "sonnet": _snap(70)})
    await loop._drive({"id": "bd-hr", "title": "t", "spec": "s"})
    assert seen[0] == "sonnet", f"expected the opening dispatch to steer to sonnet, got {seen}"


async def test_drive_usage_poll_does_not_block_the_event_loop(monkeypatch):
    """A slow host poll must let other event-loop work run before it returns."""
    loop_mod._PROVIDER_DOWN.clear()
    seen = []
    poll_released = []
    poll_threads = []
    event_loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        raise worktree.NoChangesError("coder produced no commits")

    def _provider_usage(delegate):
        poll_threads.append(threading.get_ident())
        event_loop.call_soon_threadsafe(started.set)
        # Bounded so a regression cannot deadlock the test runner.
        poll_released.append(release.wait(timeout=2))
        return _snap(4 if delegate == "codex" else 70)

    loop, store = _rung_env(monkeypatch, _dispatch, cfg=_LADDER, cursor=0)
    monkeypatch.setattr("graph.sdk.provider_usage", _provider_usage, raising=False)
    drive = asyncio.create_task(loop._drive({"id": "bd-poll", "title": "t", "spec": "s"}))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
    finally:
        release.set()
        await asyncio.wait_for(drive, timeout=5)

    assert poll_threads and all(thread != loop_thread for thread in poll_threads)
    assert all(poll_released), "event loop could not release the SDK poll while it was waiting"
    assert seen[0] == "sonnet"


async def test_headroom_never_selects_outside_routing_exclusive(monkeypatch):
    """opus has by far the most headroom but is not in ``routing.exclusive`` — the rung is
    filtered before usage is even gathered, so opus is never queried and never dispatched."""
    loop_mod._PROVIDER_DOWN.clear()
    seen: list[str] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        raise worktree.NoChangesError("coder produced no commits")

    cfg = {
        "coders": {"standard": ["codex", "sonnet", "opus"], "reasoning": ["opus"]},
        "routing": {"exclusive": ["codex", "sonnet"]},
    }
    loop, store = _rung_env(monkeypatch, _dispatch, cfg=cfg, cursor=0)
    queried = _fake_sdk(monkeypatch, {"codex": _snap(2), "sonnet": _snap(50), "opus": _snap(99)})
    await loop._drive({"id": "bd-ex", "title": "t", "spec": "s"})
    assert seen[0] == "sonnet", f"opened outside the best-in-set sibling, got {seen}"
    assert "opus" not in seen, f"headroom must not leave routing.exclusive, got {seen}"
    assert "opus" not in queried, f"an excluded sibling must not even be queried, got {queried}"


async def test_routing_headroom_false_restores_the_round_robin_cursor(monkeypatch):
    """With steering off the card opens on the cursor sibling (codex) regardless of how
    spent its subscription is — today's behaviour exactly."""
    loop_mod._PROVIDER_DOWN.clear()
    seen: list[str] = []

    async def _dispatch(coder, wt, prompt, **kw):
        seen.append(coder)
        raise worktree.NoChangesError("coder produced no commits")

    cfg = {"coders": {"standard": ["codex", "sonnet"], "reasoning": ["opus"]}, "routing": {"headroom": False}}
    loop, store = _rung_env(monkeypatch, _dispatch, cfg=cfg, cursor=0)
    _fake_sdk(monkeypatch, {"codex": _snap(1), "sonnet": _snap(99)})
    await loop._drive({"id": "bd-off", "title": "t", "spec": "s"})
    assert seen[0] == "codex", f"headroom:false must keep the cursor, got {seen}"


def test_routing_headroom_knobs_apply_live_on_reload():
    """r5: a change to routing.headroom / prefer_sibling_at lands on the running loop
    through B3's reload, with no restart. Board scope here; project scope overrides it the
    same way routing.exclusive does."""
    loop = BoardLoop({"coders": {"standard": ["codex", "sonnet"], "reasoning": ["opus"]}})
    feature = {"id": "bd-live", "title": "t"}
    assert loop._headroom_on_for(feature) is True
    assert loop._prefer_sibling_at_for(feature) == 90

    loop.reload(
        {
            "project_board": {
                "coders": {"standard": ["codex", "sonnet"], "reasoning": ["opus"]},
                "routing": {"headroom": False, "prefer_sibling_at": 80},
            }
        }
    )
    assert loop._headroom_on_for(feature) is False
    assert loop._prefer_sibling_at_for(feature) == 80

    loop.reload(
        {
            "project_board": {
                "coders": {"standard": ["codex", "sonnet"], "reasoning": ["opus"]},
                "routing": {"headroom": True},
            }
        }
    )
    assert loop._headroom_on_for(feature) is True


def test_validate_routing_accepts_the_new_knobs_and_rejects_garbage():
    """The live-reload path (and the B2/B3 routing tools) validate routing; the new keys
    must pass and bad values must be refused with a clear message."""
    cfg = {"coders": {"standard": ["codex"]}, "routing": {"headroom": False, "prefer_sibling_at": 85}}
    validate_routing(cfg, resolve=False)  # no raise
    with pytest.raises(ValueError, match="headroom must be a boolean"):
        validate_routing({"routing": {"headroom": "maybe"}}, resolve=False)
    with pytest.raises(ValueError, match="prefer_sibling_at"):
        validate_routing({"routing": {"prefer_sibling_at": 0}}, resolve=False)
    with pytest.raises(ValueError, match="only exclusive, headroom, prefer_sibling_at"):
        validate_routing({"routing": {"nonsense": 1}}, resolve=False)
