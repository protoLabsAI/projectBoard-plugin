"""Spec-width advisory at the Ready gate (#548).

The breadth cap (#143) is the only pre-failure nudge toward smaller slices, and it counts
COUNTED files_to_modify. A conceptually wide card slips past it: ds-syt (protoContent#609)
had 4 files at difficulty medium, yet its spec asked for a radio-menu composition +
theme-safe portals for menu AND submenu + a focus handoff — and it timed out twice at
1800s. This suite pins the SPEC-WIDTH advisory that reads width out of the spec itself:
three signals (prose length, acceptance-criteria count, "and also"-style concern joins),
each reporting its measured value and threshold. It is an ADVISORY — it never refuses, so
mark_ready still succeeds on an otherwise-valid wide card — and it skips task-type beads.

Mirrors the fake-`br` unit pattern of tests/test_card_authoring_455.py (the `make_board`
fixture lives in tests/conftest.py).
"""

from __future__ import annotations

import pytest

from project_board.store import (
    SPEC_WIDTH_MAX_CHARS,
    SPEC_WIDTH_MAX_CRITERIA,
    SPEC_WIDTH_MAX_JOINS,
    BoardError,
)


class _Br:
    """A fake ``_run`` that records every call; reads return empty."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, want_json=False, with_has_more=False):
        self.calls.append(args)
        val = [] if want_json else ""
        return (val, None) if with_has_more else val

    def writes(self):
        return [c for c in self.calls if c and c[0] in ("update", "dep", "comments", "close", "create")]


def _card(fid, files=("a.py (new)",), *, state="backlog", **over):
    base = {
        "id": fid,
        "board_state": state,
        "spec": "a short spec",
        "acceptance_criteria": "- WHEN x THE SYSTEM SHALL y",
        "files_to_modify": list(files),
        "difficulty": "medium",
        "design": "",
        "depends_on": [],
        "created_at": "",
        "project": "",
        "issue_type": "feature",
    }
    base.update(over)
    return base


def _wire(monkeypatch, board, cards):
    by_id = {c["id"]: c for c in cards}
    monkeypatch.setattr(board, "get_feature", lambda fid: by_id.get(fid))
    monkeypatch.setattr(board, "list_features", lambda *a, **k: list(cards))


def _spec_width(check):
    return [a for a in check["advisories"] if a["gate"] == "spec-width"]


# ── a single tripped signal names its measured value and threshold ────────────────────


def test_long_spec_trips_the_char_signal(make_board, monkeypatch):
    br = _Br()
    b = make_board(br)
    over = SPEC_WIDTH_MAX_CHARS + 1
    _wire(monkeypatch, b, [_card("bd-1", spec="x" * over)])
    adv = _spec_width(b.ready_check("bd-1"))
    assert len(adv) == 1
    assert str(over) in adv[0]["message"] and str(SPEC_WIDTH_MAX_CHARS) in adv[0]["message"]
    assert adv[0]["fix"] == "consider splitting into cards that each deliver one concern"


def test_too_many_criteria_trips_the_criteria_signal(make_board, monkeypatch):
    b = make_board(_Br())
    n = SPEC_WIDTH_MAX_CRITERIA + 1
    ac = "\n".join(f"- WHEN x{i} THE SYSTEM SHALL y{i}" for i in range(n))
    _wire(monkeypatch, b, [_card("bd-2", acceptance_criteria=ac)])
    adv = _spec_width(b.ready_check("bd-2"))
    assert len(adv) == 1
    assert "acceptance criteria" in adv[0]["message"]
    assert str(n) in adv[0]["message"] and str(SPEC_WIDTH_MAX_CRITERIA) in adv[0]["message"]


def test_too_many_concern_joins_trips_the_joiner_signal(make_board, monkeypatch):
    b = make_board(_Br())
    n = SPEC_WIDTH_MAX_JOINS + 1
    spec = "do x" + " and also y" * n  # n occurrences of the " and also " joiner
    _wire(monkeypatch, b, [_card("bd-3", spec=spec)])
    adv = _spec_width(b.ready_check("bd-3"))
    assert len(adv) == 1
    assert "concern-joining" in adv[0]["message"]
    assert str(n) in adv[0]["message"] and str(SPEC_WIDTH_MAX_JOINS) in adv[0]["message"]


def test_the_message_names_every_signal_that_tripped(make_board, monkeypatch):
    b = make_board(_Br())
    chars, crit, joins = SPEC_WIDTH_MAX_CHARS + 1, SPEC_WIDTH_MAX_CRITERIA + 1, SPEC_WIDTH_MAX_JOINS + 1
    spec = ("x" * chars) + (" and also y" * joins)
    ac = "\n".join(f"- WHEN x{i} THE SYSTEM SHALL y{i}" for i in range(crit))
    _wire(monkeypatch, b, [_card("bd-4", spec=spec, acceptance_criteria=ac)])
    adv = _spec_width(b.ready_check("bd-4"))
    assert len(adv) == 1
    msg = adv[0]["message"]
    for needle in ("chars", "acceptance criteria", "concern-joining"):
        assert needle in msg, needle


# ── it is an advisory, never a refusal — mark_ready still succeeds ─────────────────────


def test_a_wide_card_is_advised_not_refused_and_mark_ready_succeeds(make_board, monkeypatch):
    br = _Br()
    b = make_board(br)  # files_to_modify use the `(new)` marker so phantom-paths passes
    over = SPEC_WIDTH_MAX_CHARS + 1
    _wire(monkeypatch, b, [_card("bd-5", spec="x" * over)])
    check = b.ready_check("bd-5")
    assert check["ok"] is True
    assert "spec-width" not in {r["gate"] for r in check["refusals"]}
    assert len(_spec_width(check)) == 1
    b.mark_ready("bd-5")  # the advisory must not block promotion


# ── a narrow card gets nothing ────────────────────────────────────────────────────────


def test_a_narrow_card_gets_no_spec_width_advisory(make_board, monkeypatch):
    b = make_board(_Br())
    _wire(monkeypatch, b, [_card("bd-6")])
    assert _spec_width(b.ready_check("bd-6")) == []


# ── task-type beads are skipped ───────────────────────────────────────────────────────


def test_task_type_beads_get_no_spec_width_advisory(make_board, monkeypatch):
    b = make_board(_Br())
    over = SPEC_WIDTH_MAX_CHARS + 1
    crit = SPEC_WIDTH_MAX_CRITERIA + 1
    spec = ("x" * over) + (" and also y" * (SPEC_WIDTH_MAX_JOINS + 1))
    ac = "\n".join(f"- deliver item {i}" for i in range(crit))
    # A task bead ships a deliverable, not repo edits, so it carries no files_to_modify.
    _wire(monkeypatch, b, [_card("bd-7", files=(), issue_type="task", spec=spec, acceptance_criteria=ac)])
    assert _spec_width(b.ready_check("bd-7")) == []


# ── it runs before the early return, so a file-less, dep-less card is still measured ───


def test_a_card_with_no_files_and_no_deps_is_still_measured(make_board, monkeypatch):
    b = make_board(_Br())
    over = SPEC_WIDTH_MAX_CHARS + 1
    _wire(monkeypatch, b, [_card("bd-8", files=(), spec="x" * over)])
    check = b.ready_check("bd-8")
    # files_to_modify is missing, so a coding card is refused on required-fields — but the
    # spec-width advisory is independent and must still be present (it runs before the early
    # return that fires when there are no files and no depends_on).
    assert len(_spec_width(check)) == 1
    assert "spec-width" not in {r["gate"] for r in check["refusals"]}


def test_a_task_bead_with_no_files_still_skips_spec_width(make_board, monkeypatch):
    # The companion to the above: a file-less card that IS a task bead stays clean.
    b = make_board(_Br())
    _wire(monkeypatch, b, [_card("bd-9", files=(), issue_type="task", spec="x" * (SPEC_WIDTH_MAX_CHARS + 1))])
    check = b.ready_check("bd-9")
    assert check["ok"] is True  # task beads need no files_to_modify
    assert _spec_width(check) == []


def test_mark_ready_does_not_raise_on_a_wide_card(make_board, monkeypatch):
    # Guards r2 from the mark_ready side: a wide-but-valid card promotes without a BoardError.
    b = make_board(_Br())
    _wire(monkeypatch, b, [_card("bd-10", spec="x" * (SPEC_WIDTH_MAX_CHARS + 1))])
    try:
        b.mark_ready("bd-10")
    except BoardError as e:  # pragma: no cover - fails the test with context
        pytest.fail(f"spec-width advisory wrongly blocked mark_ready: {e}")
