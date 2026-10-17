"""#549: difficulty overrides only change the fresh-build parking threshold."""

import pytest

from project_board import coder_seam, worktree

from test_timeout_decompose_378 import _CARD, _TIMEOUT, _SplitStore, _blocks, _timing_out_board


@pytest.mark.parametrize("difficulty", ["medium", " MeDiUm ", "large"])
async def test_first_fresh_timeout_parks_matching_difficulty(monkeypatch, difficulty):
    store = _SplitStore(tiers=["reasoning"])
    loop, dispatched = _timing_out_board(
        monkeypatch,
        store,
        {
            "coders": {"smart": "a", "reasoning": "b"},
            "decompose_after_timeouts_by_difficulty": {"medium": 1, "large": 1},
        },
    )
    await loop._drive({**_CARD, "difficulty": difficulty})

    assert dispatched == ["a"]
    assert store.asked == [("bd-1", 1)]
    assert store.budgets["bd-1:timeout"] == 1
    assert _blocks(store)[-1][3] == "too-wide"
    assert store.escalated == []


async def test_unconfigured_small_card_needs_two_timeouts(monkeypatch):
    store = _SplitStore()
    cfg = {"coder": "proto", "decompose_after_timeouts_by_difficulty": {"medium": 1}}
    loop, _ = _timing_out_board(monkeypatch, store, cfg)
    await loop._drive({**_CARD, "difficulty": "small"})
    assert store.asked == []
    assert store.budgets["bd-1:timeout"] == 1
    assert _blocks(store)[-1][3] == "transient"

    loop, _ = _timing_out_board(monkeypatch, store, cfg)
    await loop._drive({**_CARD, "difficulty": "small"})
    assert store.asked == [("bd-1", 2)]
    assert _blocks(store)[-1][3] == "too-wide"


async def test_zero_override_never_parks(monkeypatch):
    store = _SplitStore()
    store.budgets["bd-1:timeout"] = 10
    loop, _ = _timing_out_board(
        monkeypatch, store, {"coder": "proto", "decompose_after_timeouts_by_difficulty": {"medium": 0}}
    )
    await loop._drive({**_CARD, "difficulty": "medium"})
    assert store.asked == []
    assert store.budgets["bd-1:timeout"] == 11
    assert _blocks(store)[-1][3] == "transient"


@pytest.mark.parametrize("invalid", ["1", 1.5, None, True, [], {}])
def test_config_normalizes_keys_and_ignores_non_integers(monkeypatch, invalid):
    loop, _ = _timing_out_board(
        monkeypatch,
        _SplitStore(),
        {
            "coder": "proto",
            "decompose_after_timeouts": 3,
            "decompose_after_timeouts_by_difficulty": {" MEDIUM ": 1, "small": invalid},
        },
    )
    assert loop.decompose_after_timeouts_by_difficulty == {"medium": 1}
    assert loop._too_wide_threshold({"difficulty": " MEDIUM "}) == 1
    assert loop._too_wide_threshold({"difficulty": "small"}) == 3
    assert loop._too_wide_threshold({}) == 3


@pytest.mark.parametrize("override", [None, [], "medium: 1"])
def test_non_mapping_config_uses_global_default(monkeypatch, override):
    loop, _ = _timing_out_board(
        monkeypatch,
        _SplitStore(),
        {"coder": "proto", "decompose_after_timeouts_by_difficulty": override},
    )
    assert loop.decompose_after_timeouts_by_difficulty == {}
    assert loop._too_wide_threshold({"difficulty": "medium"}) == 2


async def test_pre_model_timeout_does_not_count_with_override(monkeypatch):
    store = _SplitStore()
    loop, _ = _timing_out_board(
        monkeypatch,
        store,
        {"coder": "proto", "decompose_after_timeouts_by_difficulty": {"medium": 1}},
        model_worked=False,
    )
    await loop._drive({**_CARD, "difficulty": "medium"})
    assert store.asked == []
    assert "bd-1:timeout" not in store.budgets
    assert _blocks(store)[-1][3] == "dispatch-infra"


@pytest.mark.parametrize("fix_kind", ["pr", "kept_worktree"])
async def test_fix_timeouts_never_count_with_override(monkeypatch, fix_kind):
    store = _SplitStore()
    store.budgets["bd-1:timeout"] = 5
    loop, dispatched = _timing_out_board(
        monkeypatch,
        store,
        {"coder": "proto", "decompose_after_timeouts_by_difficulty": {"medium": 1}},
    )
    card = {**_CARD, "difficulty": "medium"}
    if fix_kind == "pr":
        card["pr_url"] = "https://example/pr/1"
        loop._ci_feedback["bd-1"] = "CI failed: test_x"
    else:

        async def _dispatch(coder, wt, prompt, *, timeout=None, env_passthrough=()):
            dispatched.append(coder)
            coder_seam.progress_tool("bd-1", 1, {"phase": "start", "name": "Edit", "id": "t1", "input": {}})
            if len(dispatched) == 1:
                return "built it"
            raise worktree.CoderTimeout(_TIMEOUT)

        async def _gap(feature, wt, base, reply=""):
            return "missing tests"

        monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
        monkeypatch.setattr(loop, "_verify_goal", _gap)
        loop.goal_verify = True

    await loop._drive(card)
    assert len(dispatched) == (2 if fix_kind == "kept_worktree" else 1)
    assert store.asked == []
    assert store.budgets["bd-1:timeout"] == 5
    assert all(c[3] != "too-wide" for c in _blocks(store))
