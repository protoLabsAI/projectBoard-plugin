"""Board-view contract tests (#26).

The board page is a no-build, vanilla-JS HTML string (``BOARD_PAGE``) — there's no JS
runtime in the suite, so (like ``test_api`` guarding the served path) these assert the
structural contract of the page: the list view groups features into collapsible
per-state sections, and the Kanban grouping is left untouched.
"""

from __future__ import annotations

from project_board.board_view import BOARD_PAGE


def test_list_sections_cover_cols_plus_blocked_and_cancelled():
    """The list groups by COLS order + the blocked flag-state + cancelled (the second
    terminal edge), so every board state a feature can be in has a home in the list."""
    assert 'const LIST_SECTIONS = [...COLS, "blocked", "cancelled"];' in BOARD_PAGE


def test_list_groups_are_collapsible_and_persist_across_reloads():
    """A per-state header toggles its group; collapse state lives in a module-scoped Set
    so the 10s auto-reload re-render doesn't re-expand what the user closed."""
    assert "function toggleGroup(state)" in BOARD_PAGE
    assert "const COLLAPSED = new Set();" in BOARD_PAGE
    assert "window.toggleGroup = toggleGroup;" in BOARD_PAGE  # exposed for the inline onclick
    # the group header row carries the state name + a count badge, and omits empty sections
    assert 'class="grp"' in BOARD_PAGE
    assert "if (!items.length) return;" in BOARD_PAGE


def test_kanban_columns_are_unchanged():
    """#26 is the list projection only — the Kanban's 5 state columns stay as they were."""
    assert 'const COLS = ["backlog", "ready", "in_progress", "in_review", "done"];' in BOARD_PAGE
