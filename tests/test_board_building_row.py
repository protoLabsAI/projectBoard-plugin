"""The Building card's status row keeps the coder's name readable in a narrow lane.

The name (`.bld-who`) used to be the only shrinkable item in the row, so a narrow Building
lane cut "codex" to "c…" while the rung label ("reasoning") and the timer kept full width.
"""

import re

from project_board.board_view import BOARD_PAGE


def _rule(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\{([^}]*)\}", BOARD_PAGE)
    assert m, selector
    return m.group(1)


def test_coder_name_does_not_shrink_and_the_rung_label_truncates_instead():
    assert "flex:0 0 auto" in _rule(".bld-who")
    gen = _rule(".bld-gen")
    assert "min-width:0" in gen and "text-overflow:ellipsis" in gen and "flex:0 1 auto" in gen


def test_full_coder_name_is_on_hover():
    assert 'b.querySelector(".bld-who").title = i.who;' in BOARD_PAGE
