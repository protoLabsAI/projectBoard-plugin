"""Difficulty → initial model-tier mapping (bd-xqypj).

Sonnet (the ``standard`` rung) should handle small AND medium cards; only ``large``
and ``architectural`` open on a stronger rung. The default map is overridable per board
via the ``difficulty_tiers`` config key, which is validated at store construction. The
escalation ladder is unchanged — a medium card that fails on Sonnet still climbs to the
``reasoning`` rung (Opus), so the harder-than-it-looked card still gets the reasoning model.

Pure map/validation tests run host-free (``resolve_difficulty_tiers`` needs no CLI); the
end-to-end tier reads use the ``make_board`` fixture (``br`` PATH stubbed, ``_run`` faked).
"""

from __future__ import annotations

import pytest

from project_board import store
from project_board.store import DIFFICULTY_TIER, BoardError, resolve_difficulty_tiers

# The new default: small AND medium start on `standard`; only large/architectural climb.
DEFAULT_MAP = {"small": "standard", "medium": "standard", "large": "reasoning", "architectural": "advanced"}


# ── the default map (r1) ─────────────────────────────────────────────────────────


def test_default_map_moves_medium_to_standard():
    """The built-in map starts small and medium on `standard` (Sonnet); large on
    `reasoning` and architectural on `advanced`. Medium moved off `reasoning`."""
    assert DIFFICULTY_TIER == DEFAULT_MAP
    assert DIFFICULTY_TIER["medium"] == "standard"  # the whole point of the card
    assert resolve_difficulty_tiers(None) == DEFAULT_MAP
    assert resolve_difficulty_tiers({}) == DEFAULT_MAP


def test_resolve_returns_a_fresh_copy():
    """A caller's mutation of the resolved map can't corrupt the module default."""
    resolved = resolve_difficulty_tiers(None)
    resolved["medium"] = "advanced"
    assert DIFFICULTY_TIER["medium"] == "standard"


# ── per-board override + validation (r2) ─────────────────────────────────────────


def test_override_replaces_named_keys():
    """An explicit full map overrides every rung (here keeping the new default shape)."""
    override = {"small": "standard", "medium": "standard", "large": "reasoning", "architectural": "advanced"}
    assert resolve_difficulty_tiers(override) == DEFAULT_MAP


def test_override_can_send_medium_back_to_reasoning():
    """A board that wants the OLD behavior can pin medium → reasoning explicitly."""
    assert resolve_difficulty_tiers({"medium": "reasoning"})["medium"] == "reasoning"


def test_missing_key_falls_back_to_default():
    """An override naming only some difficulties keeps the default tier for the rest."""
    merged = resolve_difficulty_tiers({"large": "advanced"})
    assert merged["large"] == "advanced"  # the one key the override changed
    assert merged["small"] == "standard"  # untouched → default
    assert merged["medium"] == "standard"
    assert merged["architectural"] == "advanced"


def test_legacy_tier_aliases_are_accepted():
    """Legacy tier names (`smart`→standard, `opus`→advanced) canonicalize, matching the
    coder-ladder config's own alias handling — not a new rejection."""
    merged = resolve_difficulty_tiers({"small": "smart", "architectural": "opus"})
    assert merged["small"] == "standard"
    assert merged["architectural"] == "advanced"


def test_unknown_difficulty_is_rejected_at_load():
    """A typo'd difficulty fails fast with a clear error rather than silently mis-routing."""
    with pytest.raises(BoardError, match="unknown difficulty 'huge'"):
        resolve_difficulty_tiers({"huge": "standard"})


def test_unknown_tier_is_rejected_at_load():
    """A tier outside the ladder is rejected, naming the bad difficulty and value."""
    with pytest.raises(BoardError, match="unknown tier 'superbrain'"):
        resolve_difficulty_tiers({"medium": "superbrain"})


def test_non_mapping_override_is_rejected():
    """A malformed (non-dict) override is a config error, not silently ignored."""
    with pytest.raises(BoardError, match="must be a mapping"):
        resolve_difficulty_tiers(["medium", "standard"])


def test_board_rejects_invalid_override_on_construction(make_board):
    """The validation runs at store construction (config load), so a bad `difficulty_tiers`
    never produces a live board that mis-routes cards."""
    with pytest.raises(BoardError, match="unknown difficulty"):
        store.BeadsBoard(db=None, repo="/repo", difficulty_tiers={"gigantic": "advanced"})


# ── end-to-end tier reads through the store (r1, r2) ─────────────────────────────


def _feature(fid="bd-1", *, difficulty="medium", labels=None):
    return {"id": fid, "difficulty": difficulty, "labels": list(labels or [])}


def test_initial_tier_for_medium_is_standard_by_default(make_board, monkeypatch):
    """A default board starts a medium card on `standard` (Sonnet), not Opus."""
    b = make_board(lambda *a, **k: None)
    monkeypatch.setattr(b, "_require", lambda fid: _feature(fid, difficulty="medium"))
    assert b.difficulty_tiers == DEFAULT_MAP
    assert b.initial_tier("bd-1") == "standard"


def test_initial_tier_honors_a_per_board_override(make_board, monkeypatch):
    """A board configured with a `difficulty_tiers` override routes a medium card to the
    configured rung — here the opt-in old behavior (medium → reasoning)."""
    b = store.BeadsBoard(db=None, repo="/repo", difficulty_tiers={"medium": "reasoning"})
    monkeypatch.setattr(b, "_run", lambda *a, **k: None)
    monkeypatch.setattr(b, "_require", lambda fid: _feature(fid, difficulty="medium"))
    assert b.difficulty_tiers["medium"] == "reasoning"
    assert b.initial_tier("bd-1") == "reasoning"


# ── escalation is unchanged (r3) ─────────────────────────────────────────────────


def test_medium_card_climbs_from_standard_to_reasoning(make_board, monkeypatch):
    """A medium card that fails at its starting `standard` rung climbs the ladder: the
    next rung is `reasoning` (Opus), so a hard medium card still reaches the reasoning
    model after failing on Sonnet."""
    b = make_board(lambda *a, **k: None)
    monkeypatch.setattr(b, "comment", lambda *a, **k: None)
    monkeypatch.setattr(b, "get_feature", lambda fid: _feature(fid))
    # No tier labels yet → current_tier derives the medium start (standard).
    monkeypatch.setattr(b, "_require", lambda fid: _feature(fid, difficulty="medium", labels=[]))
    assert b.current_tier("bd-1") == "standard"
    # escalate records the failed standard attempt and advances to reasoning.
    assert b.escalate("bd-1", "gate failed: tests red") == "reasoning"


def test_current_tier_reads_the_highest_tier_label(make_board, monkeypatch):
    """Once a medium card carries a `tier:reasoning` label (from a prior escalation), the
    ladder keeps it on the reasoning rung rather than resetting to its standard start."""
    b = make_board(lambda *a, **k: None)
    monkeypatch.setattr(
        b, "_require", lambda fid: _feature(fid, difficulty="medium", labels=["tier:reasoning", "attempt:1"])
    )
    assert b.current_tier("bd-1") == "reasoning"


# ── the override reaches the loop + API store construction (r1, r2) ───────────────
# These guard the config→store WIRING, not just BeadsBoard(difficulty_tiers=...) in
# isolation. The loop and the /ci API each build their own store_kw, and get_store caches
# a board by (db, repo, base_branch) alone — so an override dropped from EITHER builder is
# silently ignored whenever that builder is the first to construct the shared board.


def test_loop_store_kw_threads_the_difficulty_tiers_override():
    """The loop hands its board the `difficulty_tiers` config key (beside
    max_files_by_difficulty); an absent key stays None so the board keeps the default."""
    from project_board.loop import BoardLoop

    override = {"small": "standard", "medium": "standard", "large": "reasoning", "architectural": "advanced"}
    assert BoardLoop({"difficulty_tiers": override})._store_kw["difficulty_tiers"] == override
    assert BoardLoop({})._store_kw["difficulty_tiers"] is None


def test_api_store_kw_threads_the_difficulty_tiers_override():
    """api._store_kw (the kwargs BOTH routers build stores with — including the /ci
    escalation path) carries the `difficulty_tiers` override; an absent key stays None,
    so the board keeps the default map."""
    from project_board import api

    override = {"medium": "reasoning"}
    assert api._store_kw({"difficulty_tiers": override})["difficulty_tiers"] == override
    assert api._store_kw({})["difficulty_tiers"] is None
    assert api._store_kw(None)["difficulty_tiers"] is None


def test_loop_store_kw_override_reaches_a_freshly_constructed_board(monkeypatch):
    """End-to-end through the real get_store seam the loop dispatches on: a board built from
    the loop's store_kw honors the per-board override (medium opts back into reasoning),
    while an unset key leaves the board on the new default (medium → standard). Proves the
    config value survives the (db, repo, base_branch)-keyed cache, not just BeadsBoard."""
    from project_board.loop import BoardLoop

    monkeypatch.setattr(store.shutil, "which", lambda *_a, **_k: "/usr/bin/br")
    monkeypatch.setattr(store, "default_db_path", lambda: "/inst/project_board/.beads/beads.db")
    monkeypatch.setattr(store, "_BOARDS", {})

    overridden = store.get_store(**BoardLoop({"difficulty_tiers": {"medium": "reasoning"}})._store_kw)
    assert overridden.difficulty_tiers["medium"] == "reasoning"

    monkeypatch.setattr(store, "_BOARDS", {})
    default = store.get_store(**BoardLoop({})._store_kw)
    assert default.difficulty_tiers["medium"] == "standard"
