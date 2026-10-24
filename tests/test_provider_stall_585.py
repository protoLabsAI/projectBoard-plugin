"""#585: long provider silence has its own durable, same-tier retry budget."""

import pytest

from project_board import coder_seam, failures, worktree
from project_board.loop.drive import _timeout_idle_s
from test_timeout_decompose_378 import _CARD, _SplitStore, _blocks, _timing_out_board


@pytest.mark.parametrize(
    "text, expected",
    [
        ("age since last tool event: 600s", 600),
        ("last tool event 601.5 seconds ago", 601.5),
        ("last tool event age=0s", 0),
        ("coder timed out after 1800s", None),
        ("age since last tool event: -600s", None),
        ("age since last tool event: nans", None),
        ("age since last tool event: 600ms", None),
        ("age since last tool event: 1e3s", None),
        ("age since last tool event: " + "9" * 400 + "s", None),
    ],
)
def test_defensive_idle_age(text, expected):
    assert _timeout_idle_s(text) == expected


def test_stall_is_transient():
    policy = failures.classify(failures.PROVIDER_STALL_CLASS)
    assert policy.category == "provider_stall"
    assert policy.retryable


def _board(monkeypatch, *, gap="600s", cfg=None, worked=True):
    store = _SplitStore(tiers=["reasoning"])
    loop, dispatched = _timing_out_board(
        monkeypatch,
        store,
        {"coders": {"smart": "a", "reasoning": "b"}, "decompose_after_timeouts": 1, **(cfg or {})},
    )
    notes = []
    monkeypatch.setattr(store, "comment", lambda fid, text: notes.append(text), raising=False)

    async def dispatch(coder, wt, prompt, **kwargs):
        dispatched.append(coder)
        if worked:
            coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Edit", "id": "t1", "input": {}})
        suffix = f"; age since last tool event: {gap}" if gap else ""
        raise worktree.CoderTimeout("coder timed out after 1800s" + suffix)

    monkeypatch.setattr(worktree, "dispatch_coder", dispatch)
    return loop, store, dispatched, notes


async def test_stall_retries_same_tier_without_counting_or_parking(monkeypatch):
    loop, store, dispatched, notes = _board(monkeypatch)
    original = worktree.dispatch_coder

    async def dispatch(*args, **kwargs):
        if dispatched:
            assert args[0] == dispatched[0] == "a"
            assert store.budgets == {"bd-1:provider-stall": 1}
            assert not store.asked and not _blocks(store) and not store.escalated
            return "built it"
        return await original(*args, **kwargs)

    async def open_pr(*args, **kwargs):
        return "https://example/pr/1"

    monkeypatch.setattr(worktree, "dispatch_coder", dispatch)
    monkeypatch.setattr(worktree, "open_pr", open_pr)
    await loop._drive(_CARD)
    assert len(notes) == 1 and notes[0].startswith("provider_stall:")
    assert "last event 600s before timeout" in notes[0]
    assert not store.escalated and not store.asked and not _blocks(store)
    assert "bd-1:provider-stall" not in store.budgets


@pytest.mark.parametrize("spent, expected", [(0, 3), (1, 2), (2, 1)])
async def test_budget_survives_restart_then_falls_through(monkeypatch, spent, expected):
    loop, store, dispatched, notes = _board(monkeypatch)
    store.budgets["bd-1:provider-stall"] = spent
    await loop._drive(_CARD)
    assert dispatched == ["a"] * expected
    assert store.budgets == {"bd-1:provider-stall": 2, "bd-1:timeout": 1}
    assert len(notes) == expected - 1
    assert store.asked == [("bd-1", 1)]
    assert _blocks(store)[-1][3] == "too-wide"
    assert "last event 600s before timeout" in _blocks(store)[-1][2]


@pytest.mark.parametrize("gap", [None, "599s", "garbage", "-900s"])
async def test_short_or_unknown_gap_preserves_parking(monkeypatch, gap):
    loop, store, dispatched, notes = _board(monkeypatch, gap=gap)
    await loop._drive(_CARD)
    assert dispatched == ["a"] and not notes
    assert store.budgets == {"bd-1:timeout": 1}
    assert store.asked == [("bd-1", 1)]
    assert _blocks(store)[-1][3] == "too-wide"
    if gap == "599s":
        assert "last event 599s before timeout" in _blocks(store)[-1][2]


@pytest.mark.parametrize("cfg", [{"provider_stall_idle_s": 601}, {"provider_stall_max": 0}])
async def test_config_controls_stall_retry(monkeypatch, cfg):
    loop, store, dispatched, notes = _board(monkeypatch, cfg=cfg)
    await loop._drive(_CARD)
    assert dispatched == ["a"] and not notes
    assert store.asked == [("bd-1", 1)]


async def test_pre_model_guard_still_blocks_with_gap_reason(monkeypatch):
    loop, store, dispatched, notes = _board(monkeypatch, worked=False)
    await loop._drive(_CARD)
    assert dispatched == ["a"] and not notes and not store.budgets
    assert _blocks(store)[-1][3] == "dispatch-infra"
    assert "last event 600s before timeout" in _blocks(store)[-1][2]


async def test_ordinary_block_reason_includes_known_gap(monkeypatch):
    loop, store, dispatched, notes = _board(
        monkeypatch, gap="42s", cfg={"coder": "proto", "coders": {}, "decompose_after_timeouts": 0}
    )
    await loop._drive(_CARD)
    assert len(dispatched) == 1 and not notes and not store.asked
    assert store.budgets == {"bd-1:timeout": 1}
    assert _blocks(store)[-1][3] == "transient"
    assert "last event 42s before timeout" in _blocks(store)[-1][2]
