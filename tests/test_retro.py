"""Tests for the loop-retro mining engine (pure: raw br dicts in → digest out)."""

from __future__ import annotations

from project_board.retro import classify, mine_feature, summarize


def _c(text):
    return {"text": text}


def test_classify_buckets_known_failure_classes():
    assert classify("CI failed: golden field map out of sync") == "golden-map / config field"
    assert classify("F841 Local variable assigned but never used") == "lint (ruff)"
    assert classify("2 files would be reformatted") == "lint (ruff)"
    assert classify("sqlite3.OperationalError: table writes has no column named blob") == "test schema / fixture"
    assert classify("manifest and pyproject versions drifted") == "version drift"
    assert classify("rebase conflict with main") == "rebase / merge conflict"
    assert classify("lint-imports: forbidden_modules") == "import contracts / layering"
    assert classify("coder produced no commits vs base") == "coder produced no diff"
    assert classify("something totally novel") == "other"


def test_mine_feature_extracts_attempts_tiers_and_blocked():
    feat = {
        "id": "bd-x",
        "title": "do a thing",
        "status": "closed",
        "labels": ["diff:medium", "blocked"],
        "comments": [
            _c("attempt 1 (tier=reasoning): CI failed: golden field map out of sync"),
            _c("attempt 2 (tier=opus): CI failed: F841 unused variable"),
            _c("blocked: CI failing at the top model tier — needs triage: http://pr/1"),
        ],
    }
    m = mine_feature(feat)
    assert m["n_attempts"] == 2
    assert m["tiers"] == ["reasoning", "opus"]
    assert m["escalated"] is True  # climbed reasoning → opus
    assert m["diff"] == "medium"
    assert m["blocked"] is True
    assert "top model tier" in m["blocked_reason"]
    # both failure classes surface (deduped, sorted)
    assert set(m["classes"]) == {"golden-map / config field", "lint (ruff)"}


def test_mine_feature_clean_build_has_no_attempts_or_classes():
    feat = {"id": "bd-ok", "title": "clean", "status": "closed", "labels": ["diff:small"], "comments": []}
    m = mine_feature(feat)
    assert m["n_attempts"] == 0 and m["escalated"] is False and m["blocked"] is False
    assert m["classes"] == []


def test_summarize_ranks_recurring_classes_with_examples_and_rates():
    feats = [
        {  # golden-map, escalated, blocked
            "id": "a",
            "title": "A",
            "status": "closed",
            "labels": ["blocked"],
            "comments": [
                _c("attempt 1 (tier=reasoning): CI failed: golden field map out of sync"),
                _c("attempt 2 (tier=opus): CI failed: golden field map out of sync"),
                _c("blocked: top tier failed"),
            ],
        },
        {  # golden-map again (recurring), single attempt, not blocked
            "id": "b",
            "title": "B",
            "status": "closed",
            "labels": [],
            "comments": [_c("attempt 1 (tier=reasoning): CI failed: golden field map drift")],
        },
        {  # lint, multi-attempt, not blocked
            "id": "c",
            "title": "C",
            "status": "closed",
            "labels": [],
            "comments": [
                _c("attempt 1 (tier=smart): CI failed: F841 unused"),
                _c("attempt 2 (tier=smart): CI failed: ruff would reformat"),
            ],
        },
        {"id": "d", "title": "D", "status": "closed", "labels": [], "comments": []},  # clean
    ]
    s = summarize(feats)
    assert s["n_features"] == 4
    # golden-map is the top recurring class (2 features), lint second (1)
    classes = [c["class"] for c in s["recurring_classes"]]
    assert classes[0] == "golden-map / config field"
    assert s["recurring_classes"][0]["count"] == 2
    assert "golden" in s["recurring_classes"][0]["example"].lower()
    assert "lint (ruff)" in classes
    # rates: 1 of 4 blocked, 1 of 4 escalated (a only), 2 of 4 multi-attempt (a, c)
    assert s["block_rate"] == 0.25
    assert s["escalation_rate"] == 0.25
    assert s["multi_attempt_rate"] == 0.5
    assert [b["id"] for b in s["blocked_features"]] == ["a"]


def test_summarize_empty_board_is_zeroed_not_divide_by_zero():
    s = summarize([])
    assert s["n_features"] == 0
    assert s["recurring_classes"] == [] and s["block_rate"] == 0.0
