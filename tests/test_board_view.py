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


# ── blocked → in_progress → rest sort (#201/#223): the view's comparator matches
# list_features ─────────────────────────────────────────────────────────────────


def test_features_sort_puts_blocked_first_then_in_progress_then_priority_then_id():
    """The client re-sorts /features on load, so its comparator must match the
    store's blocked-first, in_progress-second, priority-asc, id-tiebreak order —
    a blocked card never drowns mid-column, and what's actively building sits
    right below it."""
    assert (
        ".sort((a,b) => (a.blocked?0:a.state==='in_progress'?1:2) - (b.blocked?0:b.state==='in_progress'?1:2) || a.priority - b.priority || a.id.localeCompare(b.id));"
        in BOARD_PAGE
    )


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


# ── drawer scroll isolation (#218): drawer scroll must not leak to the board ────


def test_drawer_body_contains_its_own_scroll_chain():
    """Hitting the end of the drawer's scrollable body must not chain the scroll
    through the scrim to the board — the contain lives on the .db rule itself."""
    assert (
        "#drawer .db{padding:var(--pl-space-3) var(--pl-space-4);"
        "overflow:auto;flex:1;overscroll-behavior:contain}" in BOARD_PAGE
    )


def test_page_scroll_locks_while_drawer_is_open_and_unlocks_on_close():
    """Belt-and-suspenders with overscroll-behavior: openMonitor locks the page
    (body.drawer-open → overflow:hidden); closeMonitor is the single close routine
    (button, scrim, Esc all route through it), so every close path unlocks."""
    assert "body.drawer-open{overflow:hidden}" in BOARD_PAGE
    assert 'document.body.classList.add("drawer-open")' in BOARD_PAGE
    assert 'document.body.classList.remove("drawer-open")' in BOARD_PAGE


# ── Task-type cards (#217): document icon, deliverable, submit + approve/reject ──
#
# A task-type feature (issue_type == "task") rides the SAME board lanes/ordering/deps
# as a coding feature but ships a deliverable instead of a PR — so the card gets a
# distinct document icon, in_progress carries a submit form, in_review the deliverable
# text + Approve/Reject, and it never opens the coder monitor.


def test_task_cards_are_marked_by_issue_type_with_a_document_icon():
    """A task is told apart by issue_type; the card wears a document icon and a dashed
    left border, stamped onto the title in BOTH the Kanban card and the list row."""
    assert 'const TASK_TYPE = "task";' in BOARD_PAGE
    assert "const isTask = (f) => f.issue_type === TASK_TYPE;" in BOARD_PAGE
    assert "const DOC_ICON =" in BOARD_PAGE
    assert "doc-ico-w" in BOARD_PAGE
    assert ".card--task{border-left-style:dashed}" in BOARD_PAGE
    assert '(isTask(f)?" card--task":"")' in BOARD_PAGE
    # the icon rides the title in both projections (Kanban card + list row)
    assert BOARD_PAGE.count("docIco(f)+esc(f.title)") == 2


def test_task_in_review_shows_deliverable_text_and_ref_not_a_pr_link():
    """in_review task card renders the `deliverable` projection field and its external
    ref (surfaced as pr_url) as a plain 'ref ↗' link — never the coder 'PR ↗'. Both
    the Kanban card and the list row route their footer through taskFoot()."""
    assert 'class="deliv"' in BOARD_PAGE
    assert "esc(f.deliverable)" in BOARD_PAGE
    assert "function taskRef(f)" in BOARD_PAGE
    assert ">ref ↗<" in BOARD_PAGE
    assert "function taskFoot(f){ return isTask(f) ? taskRef(f) : pr(f); }" in BOARD_PAGE
    assert BOARD_PAGE.count("taskFoot(f)") == 3  # definition + kanban footer + list footer


def test_task_in_progress_shows_a_submit_deliverable_form():
    """in_progress task card carries a submit form: a text area + an optional ref URL
    field + a Submit button that POSTs /deliver with {text, ref}."""
    assert 'id="tdtext-' in BOARD_PAGE  # deliverable text area
    assert 'id="tdref-' in BOARD_PAGE  # optional ref URL field
    assert ">Submit deliverable<" in BOARD_PAGE
    assert 'data-deliver="' in BOARD_PAGE
    assert "function submitDeliver(fid)" in BOARD_PAGE
    assert '"/deliver", {text: text, ref: ref}' in BOARD_PAGE


def test_task_in_review_shows_approve_and_reject_controls():
    """in_review task card shows Approve + Reject; the buttons carry the data-* verbs
    the delegated click listener resolves."""
    assert ">Approve<" in BOARD_PAGE
    assert ">Reject<" in BOARD_PAGE
    assert 'data-approve="' in BOARD_PAGE
    assert 'data-reject-toggle="' in BOARD_PAGE
    assert 'data-reject="' in BOARD_PAGE


def test_approve_posts_verify_with_approved_true():
    assert "function approveTask(fid)" in BOARD_PAGE
    assert '"/verify", {approved: true}' in BOARD_PAGE


def test_reject_opens_feedback_and_posts_verify_approved_false_with_feedback():
    """Reject expands a feedback textarea (its open-state lives in a module-scoped Set
    so the 10s auto-reload keeps it open); sending POSTs approved=false + feedback and
    collapses the form."""
    assert "const REJECT_OPEN = new Set();" in BOARD_PAGE
    assert "function toggleReject(fid)" in BOARD_PAGE
    assert 'id="trtext-' in BOARD_PAGE  # the feedback textarea
    assert "function rejectTask(fid)" in BOARD_PAGE
    assert '"/verify", {approved: false, feedback: feedback}' in BOARD_PAGE
    assert "REJECT_OPEN.delete(fid); await load();" in BOARD_PAGE


def test_verify_error_slot_is_present_on_the_approve_path():
    """Regression for the review finding: the error slot (terr-<id>) is emitted OUTSIDE
    the reject-form branch, so a failed Approve (reject form collapsed) still surfaces
    its error instead of being silently dropped by taskErr's `if (el)` guard. Both
    action states (in_progress form + in_review) own a slot — two `id="terr-` sites."""
    assert "return deliv + acts + '<div class=\"terr\" id=\"terr-'+id+'\"></div>';" in BOARD_PAGE
    assert "function taskErr(fid, e)" in BOARD_PAGE
    assert BOARD_PAGE.count('id="terr-') == 2


def test_task_cards_never_open_the_coder_monitor():
    """A task never dispatches a coder, so neither its Kanban card nor its list row gets
    the data-mon monitor handle — the gate excludes tasks in BOTH projections, and the
    monitor affordance keys on the task-excluding `mon`, not raw in_progress."""
    assert "const mon = live && !isTask(f);" in BOARD_PAGE  # kanban
    assert 'const mon = f.state==="in_progress" && !isTask(f);' in BOARD_PAGE  # list
    assert '(mon?" card--live":"")' in BOARD_PAGE


def test_task_action_buttons_are_delegated_like_the_monitor():
    """The board's first mutation UI — delegated clicks keyed on data-* verbs, no dead
    window.* globals (same discipline as the data-mon monitor handle)."""
    assert 'e.target.closest("[data-deliver],[data-approve],[data-reject],[data-reject-toggle]")' in BOARD_PAGE
    assert 'submitDeliver(act.getAttribute("data-deliver"))' in BOARD_PAGE
    assert 'approveTask(act.getAttribute("data-approve"))' in BOARD_PAGE
    assert 'rejectTask(act.getAttribute("data-reject"))' in BOARD_PAGE
    assert 'toggleReject(act.getAttribute("data-reject-toggle"))' in BOARD_PAGE


def test_task_review_lane_posts_through_a_json_apiPost_helper():
    """Reads go through `api`; the task lane's writes go through `apiPost` — the same
    slug-aware authed fetch, a JSON body, and the same readable-error decode."""
    assert "const apiPost = async (p, body) =>" in BOARD_PAGE
    assert 'method: "POST"' in BOARD_PAGE
    assert 'headers: {"content-type": "application/json"}' in BOARD_PAGE
    assert "body: JSON.stringify(body || {})" in BOARD_PAGE


def test_task_cards_share_lanes_and_ordering_with_coding_features():
    """A task is filtered/sorted by the SAME comparator and column filter as a coding
    feature — no task-only lane, no separate ordering — so it lands in its board state
    with the shared blocked-first / in_progress-second / priority order."""
    # one filter/sort path for every feature; task-ness only changes card CHROME
    assert "FEATURES.filter(f => f.state === state)" in BOARD_PAGE
    assert (
        ".sort((a,b) => (a.blocked?0:a.state==='in_progress'?1:2) - (b.blocked?0:b.state==='in_progress'?1:2) || a.priority - b.priority || a.id.localeCompare(b.id));"
        in BOARD_PAGE
    )
