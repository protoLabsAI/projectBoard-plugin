"""The review gate hands the panel the card the PR implements (bd-nfd6g.16 investigation).

The panel used to see only ``{pr, repo, prior_findings}``: it judged the diff with no sight of
what the card asked for, so an out-of-scope finding and a defect in the requested change looked
the same and both cost a fix round. Now the gate renders the card's title, task, acceptance
criteria and starting files into a ``card`` workflow input (undeclared inputs pass through the
host's workflow engine, so an older recipe just ignores it) and into the a2a reviewer prompt.
"""

from __future__ import annotations

from project_board.loop.reconcile import _REVIEW_CARD_FIELD_CAP, _review_card_text

from tests.test_loop import _GateStore, _inject_fake_findings
from tests.test_review_history_514 import _PR, _blocker, _pin_gate_seams, _runtime_loop  # noqa: F401 — autouse fixture

CARD = {
    "id": "bd-1",
    "title": "Wire Work source-chat actions",
    "spec": "Open source chat navigates to the decision's session.",
    "acceptance_criteria": "WHEN Open source chat is pressed THE SYSTEM SHALL switch to that session",
    "files_to_modify": ["apps/web/src/work/WorkView.tsx"],
}


class _CardStore(_GateStore):
    def get_feature(self, fid):
        return dict(CARD)


async def test_review_workflow_receives_the_card(monkeypatch):
    _inject_fake_findings(monkeypatch)
    loop, seen = _runtime_loop(monkeypatch, ["[]"])
    await loop._review_gate(_CardStore(), "bd-1", _PR, "/repo")
    card = seen[0]["card"]
    assert "Wire Work source-chat actions" in card
    assert "SHALL switch to that session" in card
    assert "- apps/web/src/work/WorkView.tsx" in card
    assert "not a fence" in card and "out-of-scope" in card
    # transient per review: nothing lingers once the gate finishes
    assert loop._review_card == {}


async def test_no_card_input_when_the_card_is_unreadable(monkeypatch):
    class _Broken(_GateStore):
        def get_feature(self, fid):
            raise RuntimeError("br hiccup")

    _inject_fake_findings(monkeypatch)
    loop, seen = _runtime_loop(monkeypatch, [_blocker("still reviewed")])
    await loop._review_gate(_Broken(), "bd-1", _PR, "/repo")
    assert "card" not in seen[0]  # the review still ran, just without the context


def test_card_text_bounds_each_field_and_skips_empty_cards():
    assert _review_card_text(None) == ""
    assert _review_card_text({"files_to_modify": ["a.py"]}) == ""
    long = _review_card_text({**CARD, "spec": "x" * (_REVIEW_CARD_FIELD_CAP * 3)})
    assert "…(truncated)" in long
    assert "SHALL switch to that session" in long  # AC survives a long task
