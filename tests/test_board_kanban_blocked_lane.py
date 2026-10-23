"""A blocked card stays on the Kanban, in the lane of the lifecycle it was blocked in.

`blocked` is a board state with no lane, so filtering lanes on `f.state` dropped every
blocked card from the Kanban: a terminal-blocked in-review card vanished while five
dependents sat "waiting on deps" in On Deck with nothing visible to wait on.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from project_board.board_view import BOARD_PAGE
from tests.test_coder_monitor_v2 import _extract_fn


def test_kanban_lanes_and_drag_home_use_the_card_lane():
    assert "FEATURES.filter(f => laneOf(f) === state)" in BOARD_PAGE
    assert "FEATURES.filter(f => f.state === state)" not in BOARD_PAGE
    assert "const home = {lane: laneOf(f), index: [...LANES[laneOf(f)].list.children].indexOf(n)};" in BOARD_PAGE
    assert "home, over: home.lane," in BOARD_PAGE
    assert "LANES[f.state]" not in BOARD_PAGE  # no lane lookup by raw state: LANES has no "blocked"


def test_a_blocked_card_in_building_is_not_live_and_not_movable():
    # The lane is placement only: liveness (coder monitor) and drag read the card's state.
    fn = BOARD_PAGE[BOARD_PAGE.index("function updateCard(") :]
    fn = fn[: fn.index("\n}\n")]
    assert 'const live = f.state === "in_progress";' in fn
    assert "const owned = LOOP_OWNED.has(state) || blockedHere;" in fn


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "feature, lane",
    [
        ({"state": "blocked", "bead_status": "in_progress", "labels": ["blocked", "in-review"]}, "in_review"),
        ({"state": "blocked", "bead_status": "in_progress", "labels": ["blocked"]}, "in_progress"),
        ({"state": "blocked", "bead_status": "open", "labels": ["blocked", "ready"]}, "ready"),
        ({"state": "blocked", "bead_status": "open", "labels": ["blocked"]}, "backlog"),
        ({"state": "blocked"}, "backlog"),
        ({"state": "blocked", "bead_status": "deferred", "labels": ["blocked", "ready"]}, "backlog"),
        ({"state": "in_review", "bead_status": "in_progress", "labels": ["in-review"]}, "in_review"),
        ({"state": "done"}, "done"),
    ],
)
def test_lane_of(feature, lane):
    js = _extract_fn("laneOf") + f"\nconsole.log(JSON.stringify(laneOf({json.dumps(feature)})));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == lane
