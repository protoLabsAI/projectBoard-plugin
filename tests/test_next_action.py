"""#208 — the in_review sub-state the PM was never told about.

Both cards were ``in_review`` + ``review-clean``, CI green, ``auto_merge`` off: the
only thing between them and done was a human merge, and ``board_list`` gave the PM
nothing that said so (it re-offered a review the gate had already cleared). Now every
in_review row carries ``next_action`` / ``awaiting_merge`` / ``next_action_hint`` —
derived from the review sub-state labels + the board's merge posture by
``store.merge_posture``, the SAME decoding the loop's auto-merge edge uses — in the
``board_list`` tool, the ``/features`` payload, and the console chip. Labels + config
only: no per-row network.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_board as pb
from project_board import api, store
from project_board.loop import BoardLoop
from project_board.store import annotate_next_action, merge_posture, pr_number

PR = "https://github.com/o/r/pull/42"


def _feat(state="in_review", labels=(), *, blocked=False, pr=PR, fid="bd-1"):
    return {
        "id": fid,
        "title": fid,
        "board_state": state,
        "blocked": blocked,
        "labels": list(labels),
        "pr_url": pr,
        "priority": 2,
        "difficulty": "",
    }


# ── merge_posture: the derivation table ─────────────────────────────────────────


@pytest.mark.parametrize(
    "labels, blocked, auto_merge, review_gate, want, awaiting",
    [
        # the #208 case: reviewed, nothing board-side in the way, loop will NOT merge
        (["review-clean"], False, False, True, "awaiting-merge (auto_merge off)", True),
        # review gate off: no verdict to wait for — auto_merge off still means a human merges
        ([], False, False, False, "awaiting-merge (auto_merge off)", True),
        # auto_merge on: the loop merges once GitHub is CLEAN — nothing for a human to do
        (["review-clean"], False, True, True, "auto-merge pending", False),
        ([], False, True, False, "auto-merge pending", False),
        # the review sub-states
        (["review-pending"], False, False, True, "review in progress", False),
        (["review-pending"], False, True, True, "review in progress", False),
        (["changes-requested"], False, False, True, "changes requested", False),
        # gate on, never recorded a clean verdict (inert gate / pre-upgrade card) — not reviewed
        ([], False, False, True, "awaiting review verdict (no review-clean)", False),
        # operator veto / blocked win over everything
        (["review-clean", "merge-hold"], False, False, True, "merge-hold (operator veto)", False),
        (["review-clean"], True, False, True, "blocked", False),
    ],
)
def test_merge_posture_next_action_table(labels, blocked, auto_merge, review_gate, want, awaiting):
    p = merge_posture(_feat(labels=labels, blocked=blocked), auto_merge=auto_merge, review_gate=review_gate)
    assert p["next_action"] == want
    assert p["awaiting_merge"] is awaiting
    if awaiting:
        assert p["next_action_hint"] == "auto_merge is off — merge #42 or turn it on in Settings ▸ Project Board"
    else:
        assert p["next_action_hint"] == ""


@pytest.mark.parametrize("state", ["backlog", "ready", "in_progress", "done", "cancelled"])
def test_merge_posture_is_empty_outside_in_review(state):
    p = merge_posture(_feat(state=state, labels=["review-clean"]), auto_merge=False, review_gate=True)
    assert p["next_action"] == "" and p["awaiting_merge"] is False and p["next_action_hint"] == ""
    assert p["blockers"] == [f"state={state}"]


def test_merge_posture_hint_without_a_pr_number_says_the_pr():
    p = merge_posture(_feat(labels=["review-clean"], pr=""), auto_merge=False, review_gate=True)
    assert p["awaiting_merge"] is True
    assert p["next_action_hint"] == "auto_merge is off — merge the PR or turn it on in Settings ▸ Project Board"


def test_pr_number_parses_github_pull_urls():
    assert pr_number("https://github.com/o/r/pull/42") == "42"
    assert pr_number("https://github.com/o/r/pull/42/files") == "42"
    assert pr_number("https://github.com/o/r/pull/42#issuecomment-1") == "42"
    assert pr_number("https://github.com/o/r/issues/42") == ""
    assert pr_number("") == ""


def test_merge_posture_blockers_are_the_loops_board_side_reasons():
    """One source of truth: `_auto_merge_blockers`' board-side half IS merge_posture —
    the same phrases the loop logs for a parked PR."""
    f = _feat(labels=["review-pending", "merge-hold"], blocked=True)
    assert merge_posture(f, auto_merge=True, review_gate=True)["blockers"] == [
        "blocked",
        "merge-hold",
        "review in progress / changes requested",
    ]
    assert merge_posture(_feat(labels=[]), auto_merge=True, review_gate=True)["blockers"] == ["no review-clean verdict"]
    assert merge_posture(_feat(labels=[]), auto_merge=True, review_gate=False)["blockers"] == []


async def test_loop_auto_merge_blockers_reuse_merge_posture(monkeypatch):
    seen = []
    real = store.merge_posture

    def _spy(feature, **kw):
        seen.append(kw)
        return real(feature, **kw)

    monkeypatch.setattr("project_board.loop.merge_posture", _spy)
    loop = BoardLoop({"auto_merge": True, "review_gate": True})
    why = await loop._auto_merge_blockers(None, _feat(labels=["review-pending"]), PR, "/repo")
    assert seen == [{"auto_merge": True, "review_gate": True}]
    assert why == ["review in progress / changes requested"]


# ── annotate_next_action: config spellings ──────────────────────────────────────


@pytest.mark.parametrize(
    "raw, on", [(True, True), ("true", True), ("on", True), (False, False), ("false", False), ("", False)]
)
def test_annotate_reads_auto_merge_in_every_spelling(raw, on):
    (row,) = annotate_next_action([_feat(labels=["review-clean"])], {"auto_merge": raw, "review_gate": True})
    assert row["awaiting_merge"] is (not on)
    assert row["next_action"] == ("auto-merge pending" if on else "awaiting-merge (auto_merge off)")


def test_annotate_leaves_non_review_rows_untouched():
    rows = annotate_next_action([_feat(state="ready"), _feat(state="done", fid="bd-2")], {})
    assert all(not {"next_action", "awaiting_merge", "next_action_hint"} & set(r) for r in rows)


def test_annotate_defaults_match_the_loop_defaults():
    """auto_merge off + review_gate off are the manifest defaults — a default board's
    reviewed card IS awaiting a human merge."""
    (row,) = annotate_next_action([_feat()], {})
    assert row["next_action"] == "awaiting-merge (auto_merge off)" and row["awaiting_merge"] is True
    assert "merge #42" in row["next_action_hint"]


# ── board_list rows ─────────────────────────────────────────────────────────────


class _Store:
    def __init__(self, feats):
        self.feats = feats

    def list_features(self, state=None, include_archived=False):
        return [dict(f) for f in self.feats if state is None or f["board_state"] == state]


def _list_tool(cfg=None):
    return {t.name: t for t in pb._board_tools(cfg or {})}["board_list"]


def test_board_list_rows_carry_next_action_and_the_merge_hint(monkeypatch):
    fake = _Store(
        [
            _feat(labels=["review-clean"], fid="bd-1"),
            _feat(labels=["review-pending"], fid="bd-2", pr="https://github.com/o/r/pull/7"),
            _feat(state="backlog", fid="bd-3", pr=""),
        ]
    )
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    rows = {r["id"]: r for r in json.loads(_list_tool({"auto_merge": False, "review_gate": True}).invoke({}))}
    assert rows["bd-1"]["next_action"] == "awaiting-merge (auto_merge off)"
    assert rows["bd-1"]["awaiting_merge"] is True
    assert rows["bd-1"]["next_action_hint"] == "auto_merge is off — merge #42 or turn it on in Settings ▸ Project Board"
    assert rows["bd-2"]["next_action"] == "review in progress" and rows["bd-2"]["awaiting_merge"] is False
    assert "next_action_hint" not in rows["bd-2"]
    # non-review rows carry none of the three keys (the row shape stays lean)
    assert not {"next_action", "awaiting_merge", "next_action_hint"} & set(rows["bd-3"])


def test_board_list_with_auto_merge_on_never_says_awaiting_merge(monkeypatch):
    fake = _Store([_feat(labels=["review-clean"])])
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    (row,) = json.loads(_list_tool({"auto_merge": True, "review_gate": True}).invoke({}))
    assert row["next_action"] == "auto-merge pending" and row["awaiting_merge"] is False


def test_board_list_docstring_tells_the_pm_to_lead_with_awaiting_merge():
    doc = " ".join(_list_tool().description.split())  # the docstring wraps across lines
    assert "awaiting-merge (auto_merge off)" in doc
    assert "LEAD your status report" in doc
    assert "NOT a re-review" in doc


# ── the /features payload ───────────────────────────────────────────────────────


def _client(monkeypatch, feats, cfg):
    monkeypatch.setattr(api, "get_store", lambda **_kw: _Store(feats))
    app = FastAPI()
    app.include_router(api.build_data_router(cfg), prefix="/api/plugins/project_board")
    return TestClient(app)


def test_features_payload_carries_next_action_for_the_console(monkeypatch):
    c = _client(
        monkeypatch, [_feat(labels=["review-clean"]), _feat(state="ready", fid="bd-2", pr="")], {"auto_merge": False}
    )
    feats = {f["id"]: f for f in c.get("/api/plugins/project_board/features").json()["features"]}
    assert feats["bd-1"]["next_action"] == "awaiting-merge (auto_merge off)"
    assert feats["bd-1"]["awaiting_merge"] is True
    assert "merge #42" in feats["bd-1"]["next_action_hint"]
    # rows in any other state keep their existing shape — no empty keys stamped
    assert not {"next_action", "awaiting_merge", "next_action_hint"} & set(feats["bd-2"])


def test_features_payload_respects_the_boards_merge_posture(monkeypatch):
    c = _client(monkeypatch, [_feat(labels=["review-clean"])], {"auto_merge": True, "review_gate": True})
    (f,) = c.get("/api/plugins/project_board/features").json()["features"]
    assert f["next_action"] == "auto-merge pending" and f["awaiting_merge"] is False


# ── the console chip ────────────────────────────────────────────────────────────


def test_board_page_chips_the_in_review_sub_state():
    from project_board.board_view import BOARD_PAGE

    assert "const NEXT_ACTION_CHIP = {" in BOARD_PAGE
    assert '"awaiting-merge (auto_merge off)": ["pl-badge--success", "awaiting merge"],' in BOARD_PAGE
    assert '"changes requested": ["pl-badge--warning", "changes requested"],' in BOARD_PAGE
    assert "function nextActionChip(f)" in BOARD_PAGE
    # the hint rides as the chip's tooltip, esc()'d — and `blocked` keeps its own chip
    assert "const hint = f.next_action_hint || f.next_action;" in BOARD_PAGE
    assert "title=\"'+esc(hint)+'\"" in BOARD_PAGE
    assert 'f.next_action === "blocked") return "";' in BOARD_PAGE
    # wired into flags(), which both the Kanban card and the list row render
    assert "out += nextActionChip(f);" in BOARD_PAGE
