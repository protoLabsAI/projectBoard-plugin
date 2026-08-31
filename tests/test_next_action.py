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
from project_board.store import (
    NEXT_ACTION_AWAITING_DELIVERABLE,
    NEXT_ACTION_AWAITING_VERIFICATION,
    NEXT_ACTION_FIXING_REVIEW,
    NEXT_ACTION_MERGED_VERIFY_EXHAUSTED,
    annotate_next_action,
    knob_bool,
    merge_posture,
    pr_number,
    review_fix_posture,
    task_posture,
)

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


def _task(state="in_progress", *, assignee="alice", blocked=False, fid="bd-t"):
    """A task-type bead (#217): ships a deliverable, no PR. `assignee` is its dispatch
    target — a human/unassigned one parks it in_progress awaiting an out-of-band
    delivery (#305)."""
    return {
        "id": fid,
        "title": fid,
        "board_state": state,
        "issue_type": "task",
        "assignee": assignee,
        "blocked": blocked,
        "labels": [],
        "pr_url": "",
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


def test_merge_posture_names_a_known_draft_behind_the_review_states():
    """#207 meets #208: a draft is a named next_action — ahead of the merge-posture
    cases (the merge never happens on a draft), behind the review ones (the review
    gate still runs on it). Known only via `is_draft` or a stamped `pr_draft` key —
    merge_posture never fetches it."""
    f = _feat(labels=["review-clean"])
    for am in (True, False):
        p = merge_posture(f, auto_merge=am, review_gate=True, is_draft=True)
        assert p["next_action"] == "draft (run `gh pr ready`)" and p["awaiting_merge"] is False
        assert p["next_action_hint"] == ""
    assert merge_posture({**f, "pr_draft": True}, auto_merge=False, review_gate=True)["next_action"].startswith("draft")
    assert merge_posture(f, auto_merge=False, review_gate=True, is_draft=False)["awaiting_merge"] is True
    assert merge_posture(f, auto_merge=False, review_gate=True)["awaiting_merge"] is True  # unknown ≠ draft
    # the review states win over the draft
    p = merge_posture(_feat(labels=["review-pending"]), auto_merge=False, review_gate=True, is_draft=True)
    assert p["next_action"] == "review in progress"
    # board-side blockers are unchanged by a draft (the loop reads isDraft from GitHub itself)
    assert merge_posture(f, auto_merge=True, review_gate=True, is_draft=True)["blockers"] == []


# ── merged-verify exhaustion hold (ADR 0326, #326) ───────────────────────────────

_BUDGET6 = "budget:merged-verify:6"  # the one-time exhaustion sentinel for max=5 (max+1)


def test_merge_posture_flags_merged_verify_exhaustion_under_auto_merge():
    """r1: an auto_merge card whose merged-verify budget has passed the cap (the loop's
    one-time exhaustion sentinel) reads the distinct held action + a remediation hint, in
    place of `auto-merge pending`."""
    p = merge_posture(_feat(labels=["review-clean", _BUDGET6]), auto_merge=True, review_gate=True, merged_verify_max=5)
    assert p["next_action"] == NEXT_ACTION_MERGED_VERIFY_EXHAUSTED and p["awaiting_merge"] is False
    hint = p["next_action_hint"]
    assert "board_reset_merged_verify_budget bd-1" in hint
    assert "raise merged_verify_max" in hint and "base stops moving" in hint


def test_merge_posture_budget_at_cap_is_still_auto_merge_pending():
    """The trigger is the sentinel (`> max`), not `>= max`: a card AT exactly the cap just
    re-verified and can still auto-merge (its stamp is current) — only the one-time
    sentinel (`max+1`, written once base moves at the cap) means the edge is stuck."""
    p = merge_posture(
        _feat(labels=["review-clean", "budget:merged-verify:5"]), auto_merge=True, review_gate=True, merged_verify_max=5
    )
    assert p["next_action"] == "auto-merge pending" and p["next_action_hint"] == ""


def test_merge_posture_exhaustion_is_auto_merge_only():
    """auto_merge off means a human merges — the merged-verify budget only holds the
    LOOP's edge, so an exhausted budget with auto_merge off still reads awaiting-merge."""
    p = merge_posture(_feat(labels=["review-clean", _BUDGET6]), auto_merge=False, review_gate=True, merged_verify_max=5)
    assert p["next_action"] == "awaiting-merge (auto_merge off)" and p["awaiting_merge"] is True


def test_merge_posture_unlimited_or_unset_cap_never_exhausts():
    """merged_verify_max 0 = unlimited (the loop never writes a sentinel), and the param
    defaults to 0 so an un-threaded caller (the loop's `_auto_merge_blockers`) is
    unaffected — a large budget with no/zero cap stays `auto-merge pending`."""
    big = _feat(labels=["review-clean", "budget:merged-verify:9"])
    assert (
        merge_posture(big, auto_merge=True, review_gate=True, merged_verify_max=0)["next_action"]
        == "auto-merge pending"
    )
    assert merge_posture(big, auto_merge=True, review_gate=True)["next_action"] == "auto-merge pending"


@pytest.mark.parametrize(
    "labels, blocked, is_draft, want",
    [
        (["review-clean", _BUDGET6, "merge-hold"], False, None, "merge-hold (operator veto)"),
        (["review-pending", _BUDGET6], False, None, "review in progress"),
        (["changes-requested", _BUDGET6], False, None, "changes requested"),
        (["review-clean", _BUDGET6], True, None, "blocked"),
        (["review-clean", _BUDGET6], False, True, "draft (run `gh pr ready`)"),
    ],
)
def test_merge_posture_precedence_wins_over_exhaustion(labels, blocked, is_draft, want):
    """r3: blocked, operator-veto, the review sub-states, and draft all still take
    precedence — the exhaustion hold lives inside the auto_merge branch, behind them."""
    p = merge_posture(
        _feat(labels=labels, blocked=blocked), auto_merge=True, review_gate=True, merged_verify_max=5, is_draft=is_draft
    )
    assert p["next_action"] == want


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


# ── review_fix_posture: the active review-fix round (#347) ───────────────────────


@pytest.mark.parametrize(
    "state, labels, blocked, want",
    [
        # bounced + requeued: `changes-requested` rode the requeue → an actionable action
        ("ready", ["changes-requested"], False, NEXT_ACTION_FIXING_REVIEW),
        ("in_progress", ["changes-requested"], False, NEXT_ACTION_FIXING_REVIEW),
        # blocked wins (its own posture); an exhausted card is blocked, not fixing
        ("in_progress", ["changes-requested"], True, ""),
        # no changes-requested label → never bounced (or already re-armed to review-pending)
        ("in_progress", [], False, ""),
        ("ready", ["review-pending"], False, ""),
        # in_review is merge_posture's lane, done/backlog owe no action here
        ("in_review", ["changes-requested"], False, ""),
        ("backlog", ["changes-requested"], False, ""),
        ("done", ["changes-requested"], False, ""),
    ],
)
def test_review_fix_posture_table(state, labels, blocked, want):
    p = review_fix_posture(_feat(state=state, labels=labels, blocked=blocked))
    assert p["next_action"] == want
    assert p["awaiting_merge"] is False
    if want:
        assert "review gate requested changes" in p["next_action_hint"]
    else:
        assert p["next_action_hint"] == ""


def test_review_fix_posture_never_fires_for_a_task():
    """A task-type bead ships a deliverable, not a coder PR — its active-work signal is
    task_posture, never the review-fix round (which is coding-PR only)."""
    t = {**_task(state="in_progress"), "labels": ["changes-requested"]}
    assert review_fix_posture(t)["next_action"] == ""


def test_annotate_stamps_fixing_review_on_a_bounced_coding_card():
    """r7 through the seam: a coding card the gate bounced (requeued to ready/in_progress
    with `changes-requested`) reads `fixing review findings` instead of `-`, so the PM
    sees a live fix round — while in_review / task / clean cards keep their own posture."""
    rows = annotate_next_action(
        [
            _feat(state="in_progress", labels=["changes-requested"], fid="bd-fix"),
            _feat(state="ready", labels=["changes-requested"], fid="bd-fix2"),
            _feat(state="in_review", labels=["changes-requested"], fid="bd-inrev"),
            _feat(state="ready", labels=[], fid="bd-plain"),
        ],
        {"auto_merge": False, "review_gate": True},
    )
    by = {r["id"]: r for r in rows}
    assert by["bd-fix"]["next_action"] == NEXT_ACTION_FIXING_REVIEW and by["bd-fix"]["awaiting_merge"] is False
    assert "review findings on the bead" in by["bd-fix"]["next_action_hint"]
    assert by["bd-fix2"]["next_action"] == NEXT_ACTION_FIXING_REVIEW
    assert by["bd-inrev"]["next_action"] == "changes requested"  # in_review → merge_posture's lane
    # a plain ready card (never bounced) still owes no next action
    assert not {"next_action", "awaiting_merge", "next_action_hint"} & set(by["bd-plain"])


def test_board_page_chips_the_active_review_fix_round():
    """#347: the active fix round has its own warning chip; the hint rides the tooltip."""
    from project_board.board_view import BOARD_PAGE

    assert '"fixing review findings": ["pl-badge--warning", "fixing review"],' in BOARD_PAGE


# ── annotate_next_action: config spellings ──────────────────────────────────────


@pytest.mark.parametrize(
    "raw, on", [(True, True), ("true", True), ("on", True), (False, False), ("false", False), ("", False)]
)
def test_annotate_reads_auto_merge_in_every_spelling(raw, on):
    (row,) = annotate_next_action([_feat(labels=["review-clean"])], {"auto_merge": raw, "review_gate": True})
    assert row["awaiting_merge"] is (not on)
    assert row["next_action"] == ("auto-merge pending" if on else "awaiting-merge (auto_merge off)")


def test_annotate_demotes_a_red_row_to_ci_failing():
    """board_list(with_ci=True) stamped ci_status=failing: "merge #N" on a red PR is
    the wrong hint. awaiting-merge / auto-merge pending / draft → `ci failing`,
    awaiting_merge False, no hint; the review sub-states stand."""
    rows = annotate_next_action(
        [
            {**_feat(labels=["review-clean"], fid="bd-1"), "ci_status": "failing"},
            {**_feat(labels=["review-clean"], fid="bd-2"), "ci_status": "failing", "pr_draft": True},
            {**_feat(labels=["review-pending"], fid="bd-3"), "ci_status": "failing"},
            {**_feat(labels=["review-clean"], fid="bd-4"), "ci_status": "passing"},
            {**_feat(labels=["review-clean"], fid="bd-5"), "ci_status": ""},
        ],
        {"auto_merge": False, "review_gate": True},
    )
    by = {r["id"]: r for r in rows}
    assert by["bd-1"]["next_action"] == "ci failing" and by["bd-1"]["awaiting_merge"] is False
    assert by["bd-1"]["next_action_hint"] == ""
    assert by["bd-2"]["next_action"] == "ci failing"
    assert by["bd-3"]["next_action"] == "review in progress"
    assert by["bd-4"]["awaiting_merge"] is True and by["bd-5"]["awaiting_merge"] is True
    rows = annotate_next_action(
        [{**_feat(labels=["review-clean"]), "ci_status": "failing"}], {"auto_merge": True, "review_gate": True}
    )
    assert rows[0]["next_action"] == "ci failing"


def test_board_list_with_ci_reads_ci_failing_not_merge_it(monkeypatch):
    class _CiStore(_Store):
        def annotate_ci_status(self, feats):
            for f in feats:
                f["ci_status"] = "failing" if f["id"] == "bd-1" else "passing"
                f["ci_summary"] = "test (br 0.1.23)" if f["id"] == "bd-1" else ""
            return feats

    fake = _CiStore([_feat(labels=["review-clean"], fid="bd-1"), _feat(labels=["review-clean"], fid="bd-2")])
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    rows = {r["id"]: r for r in json.loads(_list_tool({"auto_merge": False}).invoke({"with_ci": True}))}
    assert rows["bd-1"]["next_action"] == "ci failing" and rows["bd-1"]["awaiting_merge"] is False
    assert "next_action_hint" not in rows["bd-1"] and rows["bd-1"]["ci_status"] == "failing"
    assert rows["bd-2"]["next_action"] == "awaiting-merge (auto_merge off)" and rows["bd-2"]["awaiting_merge"] is True


def test_knob_bool_is_the_one_helper_with_explicit_unknown_string_semantics():
    """The loop and the store used to carry two copies that disagreed on "maybe":
    one helper now — strict raises (the loop's constructor / reload), strict=False
    reads default (annotate_next_action must not break a listing on a typo)."""
    from project_board import loop as loop_mod

    assert loop_mod._knob_bool is knob_bool
    assert knob_bool({"x": "on"}, "x", False) is True and knob_bool({"x": "off"}, "x", True) is False
    assert knob_bool({"x": ""}, "x", True) is False and knob_bool({}, "x", True) is True
    with pytest.raises(ValueError):
        knob_bool({"x": "maybe"}, "x", False)
    assert knob_bool({"x": "maybe"}, "x", True, strict=False) is True
    (row,) = annotate_next_action([_feat(labels=["review-clean"])], {"auto_merge": "maybe", "review_gate": True})
    assert row["next_action"] == "awaiting-merge (auto_merge off)"  # default (off), not a crash


def test_annotate_leaves_non_review_rows_untouched():
    rows = annotate_next_action([_feat(state="ready"), _feat(state="done", fid="bd-2")], {})
    assert all(not {"next_action", "awaiting_merge", "next_action_hint"} & set(r) for r in rows)


def test_annotate_defaults_match_the_loop_defaults():
    """auto_merge off + review_gate off are the manifest defaults — a default board's
    reviewed card IS awaiting a human merge."""
    (row,) = annotate_next_action([_feat()], {})
    assert row["next_action"] == "awaiting-merge (auto_merge off)" and row["awaiting_merge"] is True
    assert "merge #42" in row["next_action_hint"]


# ── annotate: merged-verify exhaustion from the live cap (ADR 0326, #326) ─────────


def test_annotate_stamps_merged_verify_exhaustion_from_the_cfg_cap():
    """r1 through the annotate seam: the hold is read from the LIVE `merged_verify_max`
    in cfg (default 5). A card past the cap reads the held action + reset hint; one AT
    the cap stays `auto-merge pending`."""
    rows = annotate_next_action(
        [
            _feat(labels=["review-clean", _BUDGET6], fid="bd-held"),
            _feat(labels=["review-clean", "budget:merged-verify:5"], fid="bd-at-cap"),
        ],
        {"auto_merge": True, "review_gate": True},  # merged_verify_max defaults to 5
    )
    by = {r["id"]: r for r in rows}
    assert by["bd-held"]["next_action"] == NEXT_ACTION_MERGED_VERIFY_EXHAUSTED
    assert by["bd-held"]["awaiting_merge"] is False
    assert "board_reset_merged_verify_budget bd-held" in by["bd-held"]["next_action_hint"]
    assert by["bd-at-cap"]["next_action"] == "auto-merge pending"


def test_annotate_exhaustion_tracks_the_live_cap_and_the_sentinel():
    """AC4: budget at the configured cap is NOT held; the one-time sentinel (cap+1) IS.
    And raising the cap (or resetting the budget) flips a held card back to `auto-merge
    pending` with no other change — the same re-arm the loop does off the live cap."""
    held = _feat(labels=["review-clean", "budget:merged-verify:4"])
    base = {"auto_merge": True, "review_gate": True}
    (row,) = annotate_next_action([dict(held)], {**base, "merged_verify_max": 3})
    assert row["next_action"] == NEXT_ACTION_MERGED_VERIFY_EXHAUSTED  # 4 > 3 → held
    (row,) = annotate_next_action([dict(held)], {**base, "merged_verify_max": 4})
    assert row["next_action"] == "auto-merge pending"  # 4 == 4 → not past the cap
    (row,) = annotate_next_action([dict(held)], {**base, "merged_verify_max": 10})
    assert row["next_action"] == "auto-merge pending"  # cap raised → re-armed
    (row,) = annotate_next_action([dict(held)], {**base, "merged_verify_max": 0})
    assert row["next_action"] == "auto-merge pending"  # 0 = unlimited → never held


def test_annotate_demotes_the_exhaustion_hold_to_ci_failing_on_a_red_pr():
    """r3: a red PR still reads `ci failing` — the exhaustion hold is demoted exactly like
    `auto-merge pending`, so a coder-fix signal is never buried behind it."""
    (row,) = annotate_next_action(
        [{**_feat(labels=["review-clean", _BUDGET6]), "ci_status": "failing"}],
        {"auto_merge": True, "review_gate": True},
    )
    assert row["next_action"] == "ci failing" and row["awaiting_merge"] is False


def test_annotate_tolerates_a_non_int_merged_verify_max():
    """A hand-edited, unparseable cap reads as 0 (no exhaustion) rather than crashing the
    listing — the same fail-open discipline as the bool knobs."""
    (row,) = annotate_next_action(
        [_feat(labels=["review-clean", _BUDGET6])],
        {"auto_merge": True, "review_gate": True, "merged_verify_max": "banana"},
    )
    assert row["next_action"] == "auto-merge pending"


# ── task_posture: the parked-task deliverable seam (#305) ────────────────────────


@pytest.mark.parametrize(
    "state, assignee, blocked, driven, want",
    [
        ("in_progress", "alice", False, False, NEXT_ACTION_AWAITING_DELIVERABLE),  # parked on a human
        ("in_progress", "", False, False, NEXT_ACTION_AWAITING_DELIVERABLE),  # parked, unassigned
        ("in_progress", "claude", False, True, ""),  # ACP agent, live drive → working, not awaiting
        ("in_progress", "alice", True, False, ""),  # blocked → its own posture, not awaiting
        ("ready", "alice", False, False, ""),  # not yet claimed
        ("done", "alice", False, False, ""),  # terminal
        # (a delivered in_review task is `awaiting verification`, its own lane — see below)
    ],
)
def test_task_posture_table(state, assignee, blocked, driven, want):
    p = task_posture(_task(state=state, assignee=assignee, blocked=blocked), is_driven=lambda _fid: driven)
    assert p["next_action"] == want
    assert p["awaiting_merge"] is False
    if want:
        assert "board_deliver(bd-t, text=…)" in p["next_action_hint"]
        assert (assignee or "an out-of-band delivery") in p["next_action_hint"]
    else:
        assert p["next_action_hint"] == ""


# ── task_posture: the delivered-task verification seam (#217, ADR 0078) ──────────


def test_task_posture_in_review_task_awaits_verification_not_a_review_verdict():
    """r7: a DELIVERED task (in_review, no PR) awaiting its record_verification Done edge reads
    `awaiting verification` with a board_verify hint — NEVER merge_posture's coding wording
    `awaiting review verdict (no review-clean)` (ADR 0078: task verification is a SEPARATE edge)."""
    p = task_posture(_task(state="in_review", assignee="alice"), is_driven=lambda _fid: False)
    assert p["next_action"] == NEXT_ACTION_AWAITING_VERIFICATION == "awaiting verification"
    assert p["awaiting_merge"] is False
    assert "board_verify(bd-t)" in p["next_action_hint"]
    # the coding review-gate phrasing must not leak onto a task
    assert "review verdict" not in p["next_action_hint"] and "review-clean" not in p["next_action_hint"]


def test_task_posture_blocked_in_review_task_keeps_the_blocked_posture():
    """A blocked in_review task keeps its own `blocked` projection (blocked wins, as
    merge_posture projects for a blocked coding card) rather than `awaiting verification`."""
    p = task_posture(_task(state="in_review", assignee="alice", blocked=True), is_driven=lambda _fid: False)
    assert p["next_action"] == "blocked" and p["next_action_hint"] == ""


def test_annotate_stamps_awaiting_verification_on_a_delivered_task():
    """r7 through the full annotate seam: an in_review task rides the SAME row slots as a coding
    in_review card but reads `awaiting verification`, even with the review gate ON — the task is
    routed to task_posture first so merge_posture's coding verdict wording never lands on it."""
    (row,) = annotate_next_action(
        [_task(state="in_review", assignee="alice")],
        {"auto_merge": False, "review_gate": True},
        is_driven=lambda _fid: False,
    )
    assert row["next_action"] == "awaiting verification" and row["awaiting_merge"] is False
    assert "board_verify(bd-t)" in row["next_action_hint"]


def test_annotate_coding_in_review_retains_review_verdict_wording_and_precedence():
    """r7: the fix is task-scoped — a CODING feature in_review with the gate on and no clean
    verdict still reads merge_posture's `awaiting review verdict (no review-clean)`, unchanged,
    proving the task reroute never touches the coding lane's precedence."""
    (row,) = annotate_next_action(
        [{**_feat(state="in_review", labels=[]), "issue_type": "feature"}],
        {"auto_merge": False, "review_gate": True},
        is_driven=lambda _fid: False,
    )
    assert row["next_action"] == "awaiting review verdict (no review-clean)"


@pytest.mark.parametrize("state", ["backlog", "ready", "in_progress", "in_review", "done"])
def test_task_posture_never_fires_for_a_coding_feature(state):
    """#305 regression pin: the parked-task posture is task-only. A coding feature
    (issue_type "feature" or none) never reads as awaiting a deliverable — even an
    in_progress one with an assignee sitting where a parked task would."""
    for itype in ("feature", ""):
        f = {**_feat(state=state), "issue_type": itype, "assignee": "alice"}
        p = task_posture(f, is_driven=lambda _fid: False)
        assert p["next_action"] == "" and p["awaiting_merge"] is False and p["next_action_hint"] == ""


def test_annotate_stamps_awaiting_deliverable_on_a_parked_task():
    """r1: an in_progress task with a non-dispatchable assignee carries
    `awaiting deliverable` + a hint naming board_deliver and the assignee."""
    (row,) = annotate_next_action([_task(assignee="alice")], {}, is_driven=lambda _fid: False)
    assert row["next_action"] == "awaiting deliverable" and row["awaiting_merge"] is False
    assert row["next_action_hint"] == "awaiting alice — record it with board_deliver(bd-t, text=…)"


def test_annotate_leaves_a_driven_task_working_not_awaiting():
    """r2: a task actively driven by a dispatched agent (a live drive) is working — it
    gets none of the three keys stamped, exactly like a coding feature outside review."""
    (row,) = annotate_next_action([_task(assignee="claude")], {}, is_driven=lambda _fid: True)
    assert not {"next_action", "awaiting_merge", "next_action_hint"} & set(row)


def test_annotate_leaves_a_coding_feature_untouched_in_every_state():
    """r3 regression pin, via the full annotate seam: a coding feature carries no
    next_action outside in_review, whatever its state or assignee — the parked-task
    branch is gated on issue_type == task and never reaches it."""
    feats = [
        {**_feat(state="in_progress", fid="bd-1"), "issue_type": "feature", "assignee": "claude"},
        {**_feat(state="ready", fid="bd-2"), "issue_type": "feature"},
        {**_feat(state="backlog", fid="bd-3", pr=""), "issue_type": ""},
    ]
    rows = annotate_next_action(feats, {"auto_merge": False, "review_gate": True}, is_driven=lambda _fid: False)
    assert all(not {"next_action", "awaiting_merge", "next_action_hint"} & set(r) for r in rows)


def test_default_is_driven_consults_the_loops_live_drive(monkeypatch):
    """The default `is_driven` (no arg) is the loop's live-drive registry: the SAME fid
    reads `awaiting deliverable` with no drive and working with one — no per-row network,
    the pull-only signal the task asks for."""
    driven = {"bd-t"}
    monkeypatch.setattr("project_board.loop.live_drive", lambda fid: object() if fid in driven else None)
    (working,) = annotate_next_action([_task(fid="bd-t", assignee="claude")], {})
    assert not {"next_action", "awaiting_merge", "next_action_hint"} & set(working)
    driven.clear()
    (parked,) = annotate_next_action([_task(fid="bd-t", assignee="alice")], {})
    assert parked["next_action"] == "awaiting deliverable"


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


def test_board_list_surfaces_awaiting_deliverable_on_a_parked_task(monkeypatch):
    """r1 through the tool: a parked in_progress task rides the SAME `next_action` /
    `awaiting_merge` / `next_action_hint` row slots as an in_review card, and a coding
    card in the same listing keeps its merge posture."""
    fake = _Store([_task(fid="bd-t", assignee="alice"), _feat(labels=["review-clean"], fid="bd-1")])
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    rows = {r["id"]: r for r in json.loads(_list_tool({"auto_merge": False, "review_gate": True}).invoke({}))}
    assert rows["bd-t"]["next_action"] == "awaiting deliverable" and rows["bd-t"]["awaiting_merge"] is False
    assert rows["bd-t"]["next_action_hint"] == "awaiting alice — record it with board_deliver(bd-t, text=…)"
    assert rows["bd-1"]["next_action"] == "awaiting-merge (auto_merge off)"  # the feature is untouched


def test_board_list_surfaces_merged_verify_exhaustion(monkeypatch):
    """r2 through the tool: a held card carries the exhaustion next_action + the reset
    hint, so an operator sees it on the board rather than only in the loop log."""
    fake = _Store([_feat(labels=["review-clean", _BUDGET6], fid="bd-1")])
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    (row,) = json.loads(_list_tool({"auto_merge": True, "review_gate": True}).invoke({}))
    assert row["next_action"] == NEXT_ACTION_MERGED_VERIFY_EXHAUSTED and row["awaiting_merge"] is False
    assert "board_reset_merged_verify_budget bd-1" in row["next_action_hint"]


def test_next_action_follows_the_live_auto_merge_knob_without_a_restart(monkeypatch):
    """The blocker on #214: `auto_merge` is a LIVE knob (BoardLoop.reload), but
    annotate_next_action reads the register-time cfg dict. The loop, the routers and
    the tools share ONE dict (register() builds `cfg` once), and reload() writes every
    changed live knob back into `self.cfg` — so a Settings save flips `next_action`
    from "awaiting-merge" to "auto-merge pending" on the next board_list, no restart."""
    fake = _Store([_feat(labels=["review-clean"])])
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: fake)
    cfg = {"auto_merge": False, "review_gate": True, "coder": "p"}
    loop = BoardLoop(cfg)
    tool = {t.name: t for t in pb._board_tools(cfg)}["board_list"]  # the SAME dict, as register() wires it
    assert loop.cfg is cfg
    (row,) = json.loads(tool.invoke({}))
    assert row["next_action"] == "awaiting-merge (auto_merge off)" and row["awaiting_merge"] is True

    assert loop.reload({"auto_merge": True}) == {"auto_merge": (False, True)}
    (row,) = json.loads(tool.invoke({}))
    assert row["next_action"] == "auto-merge pending" and row["awaiting_merge"] is False
    assert "next_action_hint" not in row

    assert loop.reload({"auto_merge": "false"}) == {"auto_merge": (True, False)}  # the YAML spelling, coerced
    (row,) = json.loads(tool.invoke({}))
    assert row["next_action"] == "awaiting-merge (auto_merge off)"
    # …and the /features payload the console reads sees the same flip
    c = _client_with_cfg(monkeypatch, fake, cfg)
    loop.reload({"auto_merge": True})
    (f,) = c.get("/api/plugins/project_board/features").json()["features"]
    assert f["next_action"] == "auto-merge pending"


def _client_with_cfg(monkeypatch, fake, cfg):
    monkeypatch.setattr(api, "get_store", lambda **_kw: fake)
    app = FastAPI()
    app.include_router(api.build_data_router(cfg), prefix="/api/plugins/project_board")
    return TestClient(app)


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


def test_features_payload_surfaces_awaiting_deliverable_for_the_console(monkeypatch):
    """r1 through the console payload: a parked in_progress task carries
    `awaiting deliverable` + the board_deliver hint, so the page can chip it."""
    c = _client(monkeypatch, [_task(fid="bd-t", assignee="alice")], {"auto_merge": False})
    (f,) = c.get("/api/plugins/project_board/features").json()["features"]
    assert f["next_action"] == "awaiting deliverable" and f["awaiting_merge"] is False
    assert "board_deliver(bd-t, text=…)" in f["next_action_hint"] and "awaiting alice" in f["next_action_hint"]


def test_features_payload_respects_the_boards_merge_posture(monkeypatch):
    c = _client(monkeypatch, [_feat(labels=["review-clean"])], {"auto_merge": True, "review_gate": True})
    (f,) = c.get("/api/plugins/project_board/features").json()["features"]
    assert f["next_action"] == "auto-merge pending" and f["awaiting_merge"] is False


def test_features_payload_surfaces_merged_verify_exhaustion(monkeypatch):
    """r2 through the console payload: the held card carries the exhaustion next_action +
    reset hint so the page can chip it — no restart, no per-row network."""
    c = _client(monkeypatch, [_feat(labels=["review-clean", _BUDGET6])], {"auto_merge": True, "review_gate": True})
    (f,) = c.get("/api/plugins/project_board/features").json()["features"]
    assert f["next_action"] == NEXT_ACTION_MERGED_VERIFY_EXHAUSTED
    assert "board_reset_merged_verify_budget bd-1" in f["next_action_hint"]


# ── the console chip ────────────────────────────────────────────────────────────


def test_board_page_chips_the_in_review_sub_state():
    from project_board.board_view import BOARD_PAGE

    assert "const NEXT_ACTION_CHIP = {" in BOARD_PAGE
    assert '"awaiting-merge (auto_merge off)": ["pl-badge--success", "awaiting merge"],' in BOARD_PAGE
    assert '"changes requested": ["pl-badge--warning", "changes requested"],' in BOARD_PAGE
    assert '"draft (run `gh pr ready`)": ["pl-badge--warning", "draft"],' in BOARD_PAGE
    assert '"ci failing": ["pl-badge--error", "ci failing"],' in BOARD_PAGE
    assert "function nextActionChip(f)" in BOARD_PAGE
    # the hint rides as the chip's tooltip, esc()'d — and `blocked` keeps its own chip
    assert "const hint = f.next_action_hint || f.next_action;" in BOARD_PAGE
    assert "title=\"'+esc(hint)+'\"" in BOARD_PAGE
    assert 'f.next_action === "blocked") return "";' in BOARD_PAGE
    # wired into flags(), which both the Kanban card and the list row render
    assert "out += nextActionChip(f);" in BOARD_PAGE


def test_board_page_chips_the_merged_verify_exhaustion_hold():
    """r2: the ADR 0326 hold has its own warning chip; the reset/remediation hint rides
    the tooltip (the server's `next_action_hint`), so the operator never reads the log."""
    from project_board.board_view import BOARD_PAGE

    assert (
        '"auto-merge held: merged-verify budget exhausted": ["pl-badge--warning", "merge held (verify budget)"],'
        in BOARD_PAGE
    )
