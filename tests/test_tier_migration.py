"""Host-free contracts for canonical tiers at config and bead boundaries (#575)."""

import copy
from types import SimpleNamespace

import pytest
import yaml

from project_board import projects, setup_check, store, project_registry
from project_board.loop import BoardLoop
from tests.test_project_registry import _wire_host, apply_like_host


LEGACY = {"smart": ["codex", "sonnet"], "reasoning": "reasoner", "opus": "architect"}
CANONICAL = {"standard": ["codex", "sonnet"], "reasoning": "reasoner", "advanced": "architect"}


def test_load_legacy_config_at_every_consumer():
    cfg = {"coders": LEGACY, "projects": {"app": {"repo": "/app", "coders": LEGACY}}}
    original = copy.deepcopy(cfg)
    loaded = projects.resolve_project_cfg(cfg)
    assert loaded["coders"] == CANONICAL
    assert loaded["projects"]["app"]["coders"] == CANONICAL
    assert projects.resolve_projects(cfg)["app"]["coders"] == CANONICAL
    assert projects.resolve_projects({"coders": LEGACY})["default"]["coders"] == CANONICAL
    assert setup_check.uncovered_tiers(LEGACY) == []
    loop = BoardLoop(cfg)
    normalized = setup_check._normalize_coders(CANONICAL)
    assert loop.coders == normalized
    assert loop._coders_for({"project": "app"}) == normalized
    assert setup_check.stale_loop_keys(cfg, setup_check.snapshot_of(loaded)) == []
    assert cfg == original


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("old,new", projects.TIER_ALIASES.items())
def test_canonical_key_wins_even_when_blank(caplog, old, new, reverse):
    pairs = [(old, "discarded"), (new, [])]
    raw = dict(reversed(pairs) if reverse else pairs)
    assert projects.normalize_coder_tiers(raw) == {new: []}
    assert len(caplog.records) == 1
    assert "canonical keys win" in caplog.text
    assert raw[old] == "discarded"


@pytest.mark.parametrize("mixed", [False, True])
async def test_save_migrates_raw_yaml_through_host_merge_and_round_trips(monkeypatch, tmp_path, mixed):
    path = tmp_path / "config.yaml"
    section = {"coders": LEGACY, "projects": {"app": {"repo": "/app", "coders": LEGACY, "custom": True}}}
    if mixed:
        section = copy.deepcopy(section)
        for entry in (section, section["projects"]["app"]):
            entry["coders"].update({"standard": ["canonical"], "advanced": "canonical-top"})
    path.write_text(yaml.safe_dump({"project_board": section}))
    cfg = SimpleNamespace(plugin_config=yaml.safe_load(path.read_text()))
    before = projects.resolve_project_cfg(cfg.plugin_config["project_board"])
    apply = apply_like_host(cfg)

    def save(patch):
        result = apply(patch)
        path.write_text(yaml.safe_dump(cfg.plugin_config))
        return result

    _wire_host(monkeypatch, cfg, save)
    await project_registry._apply_registry(cfg.plugin_config["project_board"]["projects"])
    saved = yaml.safe_load(path.read_text())["project_board"]
    assert saved == before
    assert projects.resolve_project_cfg(saved) == before
    expected = {**CANONICAL, "standard": ["canonical"], "advanced": "canonical-top"} if mixed else CANONICAL
    assert saved["coders"] == expected
    assert saved["projects"]["app"]["coders"] == expected


@pytest.mark.parametrize(
    "labels,difficulty,current,next_tier",
    [
        (["tier:opus"], "small", "advanced", None),
        (["tier:smart"], "architectural", "standard", "reasoning"),
        (["tier:smart", "tier:advanced", "tier:reasoning"], "small", "advanced", None),
        (["tier:standard", "tier:opus"], "small", "advanced", None),
        (["tier:nonsense"], "", "standard", "reasoning"),
        ([], "architectural", "advanced", None),
        (["tier:nonsense"], "medium", "reasoning", "advanced"),
    ],
)
def test_labels_preserve_rung_priority_without_rewriting(monkeypatch, labels, difficulty, current, next_tier):
    board = object.__new__(store.BeadsBoard)
    card = {"labels": labels, "difficulty": difficulty, "priority": 1}
    before = copy.deepcopy(card)
    monkeypatch.setattr(board, "_require", lambda fid: card)
    assert board.current_tier("bd-1") == current
    assert board.next_tier(current) == next_tier
    assert card == before


def test_ladder_and_new_attempt_labels(monkeypatch):
    assert store.TIER_LADDER == ["standard", "reasoning", "advanced"]
    assert store.DIFFICULTY_TIER == {
        "small": "standard",
        "medium": "reasoning",
        "large": "reasoning",
        "architectural": "advanced",
    }
    board = object.__new__(store.BeadsBoard)
    card = {"labels": ["tier:smart"], "attempts": []}
    calls = []
    monkeypatch.setattr(board, "_require", lambda fid: card)
    monkeypatch.setattr(board, "get_feature", lambda fid: card)
    monkeypatch.setattr(board, "_run", lambda *args: calls.append(args))
    monkeypatch.setattr(board, "comment", lambda *args: None)
    assert board.escalate("bd-1", "failed") == "reasoning"
    assert calls == [
        ("update", "bd-1", "--add-label", "attempt:1", "--add-label", "tier:standard"),
        ("update", "bd-1", "--add-label", "tier:reasoning"),
    ]
    calls.clear()
    board.record_attempt("bd-1", tier="opus", outcome="failed")
    assert calls[0][-1] == "tier:advanced"
    assert board.next_tier("opus") is None
    assert board.next_tier("smart") == "reasoning"
    assert board.next_tier("nonsense") == "standard"
    assert card["labels"] == ["tier:smart"]


def test_unknown_config_keys_preserved_and_shadowed_coder_not_used():
    assert projects.normalize_coder_tiers({"custom": "x"}) == {"custom": "x"}
    cfg = {"coders": {"smart": "discarded", "standard": "chosen"}}
    assert setup_check.coder_names(cfg) == ["chosen"]
    assert not store.escalation_enabled(cfg)


def test_loop_load_warns_once_and_preserves_live_config_identity(caplog):
    cfg = {"coders": {"smart": "discarded", "standard": "chosen", "reasoning": "chosen"}}
    loop = BoardLoop(cfg)
    assert loop.cfg is cfg
    assert loop.coders == {"standard": ["chosen"], "reasoning": ["chosen"]}
    assert not loop.escalation_on
    assert len(caplog.records) == 1
