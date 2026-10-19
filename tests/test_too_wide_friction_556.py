"""A designed too-wide park must not double-log the timeout that caused it (#556)."""

import pytest

from project_board import host_friction
from project_board.loop import BoardLoop, TOO_WIDE_CLASS

from test_timeout_decompose_378 import _CARD, _TIMEOUT, _SplitStore, _blocks, _timing_out_board


@pytest.fixture
def records(monkeypatch):
    rows = []

    def record(summary, detail="", **kwargs):
        rows.append({"summary": summary, "detail": detail, **kwargs})

    # Capture before rate limiting so duplicate attempts cannot hide the regression.
    monkeypatch.setattr(host_friction, "record", record)
    return rows


@pytest.mark.parametrize("answer", [None, {}])
async def test_timeout_to_too_wide_keeps_only_drive_friction(monkeypatch, records, answer):
    class LearnedStore(_SplitStore):
        def request_decomposition(self, fid, *, timeouts, learned):
            self.learned = learned
            return super().request_decomposition(fid, timeouts=timeouts)

    store = LearnedStore(tiers=["reasoning"], answer=answer)
    loop, dispatched = _timing_out_board(monkeypatch, store, {"coders": {"smart": "a", "reasoning": "b"}})
    await loop._drive(_CARD)

    assert dispatched == ["a", "b"]
    assert len(records) == 2  # one original friction call per real CoderTimeout
    for row in records:
        assert row["summary"] == "coder drive timed out and was killed (hung or too slow)"
        assert row["severity"] == "major"
        assert row["key"] == f"{row['summary']}|bd-1"
        assert _TIMEOUT in row["detail"]
    assert store.asked == [("bd-1", 2)]
    assert "TIMED OUT" in store.learned and "Edit" in store.learned
    assert store.budgets["bd-1:timeout"] == 2
    assert loop._timeout_attempts["bd-1"] == 2
    ((_, fid, reason, category),) = _blocks(store)
    assert fid == "bd-1" and category == TOO_WIDE_CLASS
    assert _TIMEOUT in reason.splitlines()[0]
    names = store.names()
    assert names.index("request_decomposition") < names.index("flag_blocked")
    if answer is None:
        assert "bd-split" in reason
        assert names.index("flag_blocked") < names.index("mark_ready")
        assert ("mark_ready", "bd-split") in store.calls
    else:
        assert "NO split task was filed" in reason
        assert "mark_ready" not in names


@pytest.mark.parametrize("model_worked, category", [(True, "transient"), (False, "dispatch-infra")])
async def test_ordinary_timeout_still_records_drive_failure(monkeypatch, records, model_worked, category):
    store = _SplitStore()
    loop, dispatched = _timing_out_board(monkeypatch, store, {"coder": "proto"}, model_worked=model_worked)
    await loop._drive(_CARD)

    assert dispatched == ["proto"] and store.asked == []
    assert _blocks(store)[-1][3] == category
    assert len(records) == 1
    assert records[0]["summary"] == "coder drive timed out and was killed (hung or too slow)"
    assert records[0]["severity"] == "major"
    assert _TIMEOUT in records[0]["detail"]


@pytest.mark.parametrize("category", ["", "transient", "dispatch-infra", "terminal", TOO_WIDE_CLASS])
def test_block_class_controls_exclusion_without_matching_park_prose(records, category):
    store = _SplitStore()
    BoardLoop({})._flag_blocked(store, "bd-1", _TIMEOUT, category=category)

    assert _blocks(store) == [("flag_blocked", "bd-1", _TIMEOUT, category)]
    if category == TOO_WIDE_CLASS:
        assert records == []
    else:
        assert len(records) == 1
        assert records[0]["summary"] == "board loop blocked a card: a dispatch timed out"
        assert records[0]["severity"] == "major"
