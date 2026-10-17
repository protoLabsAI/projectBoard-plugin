"""Refs #549: carry the timed-out attempt's progress into the splitter's task."""

import hashlib

import pytest

from project_board import coder_seam
from project_board.loop import BoardLoop
from test_timeout_decompose_378 import _SplitStore, _asking_board, _blocks


_LEARNED = "ran ~1800s; last tool: Edit menu.tsx"


def test_learned_is_inserted_after_original_files(make_board, monkeypatch):
    board, _, created = _asking_board(make_board, monkeypatch, rows=[])
    assert board.request_decomposition("bd-8", timeouts=2, learned=_LEARNED) == {"id": "bd-9"}
    spec = created[0]["spec"]
    section = f"WHAT THE FAILED ATTEMPT(S) LEARNED\n{_LEARNED}"
    assert spec.index("ORIGINAL files_to_modify") < spec.index(section) < spec.index("CARDS THAT DEPEND")
    assert "Put the part the attempt was stuck on into its own slice." in spec


@pytest.mark.parametrize("kwargs", [{}, {"learned": ""}])
def test_empty_context_preserves_original_spec_bytes(make_board, monkeypatch, kwargs):
    board, _, created = _asking_board(make_board, monkeypatch, rows=[])
    board.request_decomposition("bd-8", timeouts=2, **kwargs)
    # SHA-256 of the pre-#549 spec for this fixture, including whitespace.
    assert hashlib.sha256(created[0]["spec"].encode()).hexdigest() == (
        "7b1b65d6e9f9c52148634766ca87b0312ee494cd8f092ea8263320f87f848f95"
    )


def test_learned_is_truncated(make_board, monkeypatch):
    board, _, created = _asking_board(make_board, monkeypatch, rows=[])
    assert board.request_decomposition("bd-8", timeouts=2, learned="x" * 4000 + "EXCESS")
    assert "x" * 4000 in created[0]["spec"]
    assert "EXCESS" not in created[0]["spec"]


def test_learned_does_not_duplicate_existing_task(make_board, monkeypatch):
    task = {"id": "bd-9", "issue_type": "task", "title": "Decompose bd-8 — timed out 2x", "board_state": "backlog"}
    board, _, created = _asking_board(make_board, monkeypatch, rows=[task])
    assert board.request_decomposition("bd-8", timeouts=3, learned=_LEARNED) == task
    assert created == []


def test_request_remains_best_effort(make_board, monkeypatch):
    board, _, _ = _asking_board(make_board, monkeypatch, rows=[])

    def fail(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(board, "create_feature", fail)
    assert board.request_decomposition("bd-8", timeouts=2, learned=_LEARNED) is None


class _LearnedStore(_SplitStore):
    def request_decomposition(self, fid, *, timeouts, learned=""):
        self.learned = learned
        return super().request_decomposition(fid, timeouts=timeouts)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("context_fails", [False, True])
async def test_park_carries_context_best_effort(monkeypatch, legacy, context_fails):
    store = _SplitStore() if legacy else _LearnedStore()
    loop = BoardLoop({"coder": "proto"})
    seen = []

    def context(fid):
        seen.append(fid)
        if context_fails:
            raise RuntimeError("snapshot unavailable")
        return _LEARNED

    monkeypatch.setattr(loop, "_timeout_escalation_context", context)
    await loop._park_for_split(store, "bd-8", 2, RuntimeError("timed out"))
    assert seen == ["bd-8"]
    assert store.asked == [("bd-8", 2)]
    assert _blocks(store)[-1][3] == "too-wide"
    assert ("mark_ready", "bd-split") in store.calls
    if not legacy:
        assert store.learned == ("" if context_fails else _LEARNED)


async def test_park_mines_elapsed_tool_and_thought_tail(monkeypatch):
    store = _LearnedStore()
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(
        coder_seam,
        "progress_snapshot",
        lambda fid: {
            "gens": [
                {
                    "elapsed_s": 1800,
                    "current_tool": {"name": "Edit", "locations": ["menu.tsx"]},
                    "thought_tail": "The menu state transition needs isolating.",
                }
            ]
        },
    )
    await loop._park_for_split(store, "bd-8", 2, RuntimeError("timed out"))
    assert "ran ~1800s" in store.learned
    assert "Edit" in store.learned and "menu.tsx" in store.learned
    assert "The menu state transition needs isolating." in store.learned
