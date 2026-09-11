"""#401's secondary finding: an unblocked card still reported the class of a block it no longer had.

bd-p8ft read `blocked: false, blocked_class: terminal` after it was unblocked. That is the
same kind of false working state as the stale snapshot: a card that is ready, described
as dead forever. `store.clear_blocked` dropped only the `dispatch-infra` class (#339), and
every other class outlived its block. The projection reported whatever class label was
left, blocked or not.

The fix is at both ends. An unblock drops the block's class, whatever it is, and records
it on the unblock's audit comment. The projection reports a class only while the card is
actually blocked, which also covers a card a pre-fix unblock or a merge left carrying one.
The #339 posture is kept: an unblock never touches the card's earned `tier:` labels. All of
this runs through the real `br`, because a label is exactly what a fake `_run` gets wrong.
"""

from __future__ import annotations

import shutil

import pytest

from project_board import store as store_mod
from project_board.store import BeadsBoard

requires_br = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH — CI installs it and sets PB_REQUIRE_BR=1",
)

_AC = "- WHEN x THE SYSTEM SHALL y"


def _ready(board: BeadsBoard, repo, title: str, path: str) -> str:
    (repo / path).write_text("x = 1\n")
    fid = board.create_feature(title, spec="s", acceptance_criteria=_AC, files_to_modify=[path])["id"]
    board.mark_ready(fid)
    return fid


def _classes(feature: dict) -> list[str]:
    return [label for label in feature["labels"] if label.startswith("blocked-class:")]


@requires_br
def test_an_unblock_drops_the_blocks_class_and_keeps_the_earned_tiers(tmp_path):
    """Whatever the class, it goes with the flag. The tiers a card climbed stay, because the
    ladder is a record of real model-capability work (#339), including for dispatch-infra."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    for cls, path in (("terminal", "a.py"), ("dispatch-infra", "b.py"), ("transient", "c.py")):
        fid = _ready(board, tmp_path, f"blocked {cls}", path)
        board._run("update", fid, "--add-label", "tier:reasoning")  # a rung it earned before the block
        board.flag_blocked(fid, f"a {cls} failure", category=cls)
        assert board.get_feature(fid)["blocked_class"] == cls  # a blocked card reports its class

        f = board.clear_blocked(fid)

        assert f["board_state"] == "ready" and not f["blocked"], cls
        assert _classes(f) == [] and f["blocked_class"] == "", f"{cls}: the class outlived its block"
        assert "tier:reasoning" in f["labels"], f"{cls}: an unblock must never touch the earned tiers"
        assert f"unblocked — cleared blocked-class:{cls}" in board.feature_comments(fid)


@requires_br
def test_a_class_left_on_an_unblocked_card_is_not_reported(tmp_path):
    """Cards unblocked before this fix, or closed by a merge, still carry a class label. The
    projection, which feeds board_get_feature, GET /features and the board view, must not
    describe a block the card does not have. A card still blocked keeps reporting its class."""
    board = BeadsBoard(repo=str(tmp_path), actor="test")
    fid = _ready(board, tmp_path, "the bd-p8ft shape", "p.py")
    board.flag_blocked(fid, "open_review expects in_progress, got 'ready'", category="terminal")
    listed = {f["id"]: f for f in board.list_features()}
    assert listed[fid]["blocked_class"] == "terminal"

    board._run("update", fid, "--remove-label", "blocked")  # what the pre-fix unblock left behind
    f = board.get_feature(fid)

    assert _classes(f) == ["blocked-class:terminal"]  # the stale label really is there…
    assert f["board_state"] == "ready" and not f["blocked"]
    assert f["blocked_class"] == ""  # …and is not reported as the card's state
    assert {r["id"]: r for r in board.list_features()}[fid]["blocked_class"] == ""
