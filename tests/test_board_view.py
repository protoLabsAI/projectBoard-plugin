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


# ── Done cap (#115): most recent 20 + the "show all" affordance ─────────────────


def test_done_column_caps_at_20_most_recent_with_a_show_all_affordance():
    """The Done column/group shows only the most recent DONE_CAP features (closed_at
    desc) so recent merges aren't buried; "show all" expands, and the expansion is
    module-scoped so the 10s auto-reload doesn't re-truncate it."""
    assert "const DONE_CAP = 20;" in BOARD_PAGE
    assert "let DONE_ALL = false;" in BOARD_PAGE
    assert "function showAllDone()" in BOARD_PAGE
    assert "window.showAllDone = showAllDone;" in BOARD_PAGE  # exposed for the inline onclick
    assert "show all (" in BOARD_PAGE  # the affordance carries the true total
    # most recent first — sorted on closed_at desc before the cap is applied
    assert '(b.closed_at||"").localeCompare(a.closed_at||"")' in BOARD_PAGE
    # both projections cap their done section through the shared slice
    assert BOARD_PAGE.count("{ const d = doneSlice(items); items = d.items; total = d.total; }") == 2


# ── live coder monitor drawer (#84) ─────────────────────────────────────────────


def test_monitor_drawer_markup_is_present():
    """A slide-over drawer + a click-away scrim, in the page's OWN HTML (an iframe —
    no console component imports)."""
    assert 'id="drawer"' in BOARD_PAGE
    assert 'id="scrim"' in BOARD_PAGE
    assert 'id="drawer-body"' in BOARD_PAGE


def test_in_progress_cards_and_rows_are_the_click_targets():
    """Only in_progress items open the monitor — the Kanban card and the list row
    both carry a data-mon handle that a delegated click listener resolves."""
    assert 'const live = state === "in_progress";' in BOARD_PAGE
    assert 'f.state==="in_progress"' in BOARD_PAGE  # the list row's data-mon guard
    assert 'e.target.closest("[data-mon]")' in BOARD_PAGE


# ── display-name map (#182): "ready" renders as "on deck" ───────────────────────


def test_state_label_map_covers_every_board_state():
    """STATE_LABEL is the operator-facing vocabulary — "ready" means "spec complete,
    queued for a coder", so the view says "on deck" (and in_progress → "building")."""
    assert "const STATE_LABEL = {" in BOARD_PAGE
    assert 'backlog: "backlog",' in BOARD_PAGE
    assert 'ready: "on deck",' in BOARD_PAGE
    assert 'in_progress: "building",' in BOARD_PAGE
    assert 'in_review: "in review",' in BOARD_PAGE
    assert 'done: "done",' in BOARD_PAGE
    assert 'blocked: "blocked",' in BOARD_PAGE
    assert 'cancelled: "cancelled",' in BOARD_PAGE


def test_state_labels_are_used_at_every_render_site_with_a_fallback():
    """All three user-facing state renders go through STATE_LABEL, falling back to
    state.replace("_"," ") for an unknown state (no crash, no `undefined` label)."""
    # Kanban column header + list group header share the same lookup-with-fallback.
    assert BOARD_PAGE.count('STATE_LABEL[state] || state.replace("_"," ")') == 2
    # List view status dot label falls back to the raw state.
    assert "esc(STATE_LABEL[f.state] || f.state)" in BOARD_PAGE
    # No render site bypasses the map: the old raw-label patterns are gone.
    assert '\'+state.replace("_"," ")+\'' not in BOARD_PAGE
    assert "esc(state.replace" not in BOARD_PAGE
    assert "esc(f.state)" not in BOARD_PAGE


def test_internal_state_names_stay_internal():
    """#182 is a view-layer label swap only — grouping, filtering, and the collapse
    toggle still key on the raw state names, so the API/store contract is untouched."""
    assert "FEATURES.filter(f => f.state === state)" in BOARD_PAGE
    assert 'data-state="' in BOARD_PAGE
    assert "'+esc(state)+'" in BOARD_PAGE  # toggleGroup's data-state carries the raw state, not the label
    assert BOARD_PAGE.count('"on deck"') == 1  # the label lives only in the map — renders go through the lookup


def test_monitor_polls_the_progress_endpoint_and_closes_on_esc_or_click_away():
    assert "function openMonitor(fid)" in BOARD_PAGE
    assert "function closeMonitor()" in BOARD_PAGE
    assert "const MON_POLL_MS = 3000;" in BOARD_PAGE  # ~3s poll while open
    assert "setInterval(pollMonitor, MON_POLL_MS)" in BOARD_PAGE
    assert '"/progress"' in BOARD_PAGE  # hits …/features/{fid}/progress
    assert 'e.key === "Escape"' in BOARD_PAGE  # Esc closes
    assert '$("scrim").addEventListener("click", closeMonitor)' in BOARD_PAGE  # click-away closes
    # Delegated listener via data-mon — no dead window.* global (panel round 2 on #89).
    assert "data-mon" in BOARD_PAGE
    assert 'openMonitor(el.getAttribute("data-mon"))' in BOARD_PAGE
