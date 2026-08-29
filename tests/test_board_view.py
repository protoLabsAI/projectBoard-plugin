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
    action states (in_progress form + in_review) own a slot — two `id="terr-` sites.
    (bd-rdmh: the deliverable moved out of taskExtra into taskDetail, so the in_review
    return is `acts + …` — the slot is still emitted outside the reject branch.)"""
    assert "return acts + '<div class=\"terr\" id=\"terr-'+id+'\"></div>';" in BOARD_PAGE
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


def test_parked_task_awaiting_deliverable_chips_on_the_card():
    """#305 (r4): an in_progress task the loop parked for an out-of-band delivery carries
    next_action "awaiting deliverable"; the page maps it to an info chip and renders it
    through the shared nextActionChip → flags(), which BOTH the Kanban card and the list
    row emit — so the chip lands on a task card, with the board_deliver hint as its
    tooltip (the server text, esc()'d)."""
    assert '"awaiting deliverable": ["pl-badge--info", "awaiting deliverable"],' in BOARD_PAGE
    assert "function nextActionChip(f)" in BOARD_PAGE
    assert "const hint = f.next_action_hint || f.next_action;" in BOARD_PAGE
    assert "title=\"'+esc(hint)+'\"" in BOARD_PAGE
    # flags() carries the chip and is rendered on both the Kanban card and the list row
    assert "out += nextActionChip(f);" in BOARD_PAGE
    assert BOARD_PAGE.count("flags(f)") == 3  # definition + Kanban card + list row


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


# ── gen log fills the drawer's vertical space (#226-UX) ──────────────────────────
#
# The coder monitor drawer already flex-fills the panel height (.db is flex:1;overflow:
# auto), but each gen section had a small hard cap, leaving whitespace below a lone gen
# — the common case, watching one live build. A solo gen card now becomes a flex column
# owning the full body height, with its recent-tools log flexing into the remaining
# space; several gens keep bounded (but larger) caps so all stay reachable by scrolling.


def test_lone_gen_becomes_a_flex_column_that_fills_the_drawer_body():
    """A single gen (`.gen:only-child` — the sole child of the flex-filled .db) turns
    into a flex column at 100% of the body height, with margin-bottom:0 so it doesn't
    spill past 100% and re-introduce a body scroll."""
    assert ".gen:only-child{display:flex;flex-direction:column;height:100%;margin-bottom:0}" in BOARD_PAGE


def test_lone_gen_tools_log_takes_the_remaining_space_uncapped():
    """The recent-tools list is the primary log the operator watches, so in the lone-gen
    layout it drops its cap (max-height:none) and grows into the leftover space (flex:1),
    keeping a sensible floor (min-height) — no dead whitespace below the card."""
    assert ".gen:only-child ul.tools{flex:1 1 auto;max-height:none;min-height:80px}" in BOARD_PAGE


def test_lone_gen_plan_and_thought_stay_bounded_but_can_shrink_and_scroll():
    """Plan/thinking keep generous caps in the lone-gen layout so they don't crowd out
    the tools log, and get min-height:0 so a long section shrinks+scrolls (its own
    overflow:auto) instead of overflowing the fixed-height card."""
    assert ".gen:only-child ul.plan{max-height:300px;min-height:0}" in BOARD_PAGE
    assert ".gen:only-child .thought{max-height:280px;min-height:0}" in BOARD_PAGE


def test_multi_gen_caps_are_raised_but_still_bounded():
    """With several gens the per-section caps stay finite (so every gen is reachable by
    scrolling the body) but are more generous than the old cramped values — thought
    120→200, plan 140→250, tools 150→300."""
    # thought's raised cap, pinned in context so it can't collide with .deliv's 120px cap
    assert "color:var(--pl-color-fg-muted);\n    max-height:200px;overflow:auto}" in BOARD_PAGE
    assert ".gen ul.plan{list-style:none;margin:0;padding:0;max-height:250px;overflow:auto}" in BOARD_PAGE
    assert ".gen ul.tools{list-style:none;margin:0;padding:0;max-height:300px;overflow:auto}" in BOARD_PAGE
    # the old cramped plan/tools caps are gone (both values are unique to those rules)
    assert "max-height:140px" not in BOARD_PAGE  # old plan
    assert "max-height:150px" not in BOARD_PAGE  # old tools
    # and the old thought cap is gone — .deliv keeps its own 120px, so pin thought's context
    assert "color:var(--pl-color-fg-muted);\n    max-height:120px;overflow:auto}" not in BOARD_PAGE


def test_input_preview_stays_compact():
    """The input preview is a peek at the current tool's args, not a log — it keeps its
    short cap in both the multi-gen and lone-gen layouts (no :only-child override)."""
    assert "max-height:48px;overflow:hidden;margin-top:2px}" in BOARD_PAGE
    assert ".gen:only-child .inprev" not in BOARD_PAGE


def test_lone_gen_fill_does_not_touch_the_drawer_body_scroll_contract():
    """The fill is driven off .gen:only-child height:100% — the .db body rule that owns
    the flex-fill + own-scroll-chain (#218) is left exactly as it was, so the body still
    scrolls (and contains its scroll) when the content overflows."""
    assert (
        "#drawer .db{padding:var(--pl-space-3) var(--pl-space-4);"
        "overflow:auto;flex:1;overscroll-behavior:contain}" in BOARD_PAGE
    )


def test_drawer_width_still_clamps_to_the_mobile_viewport():
    """The vertical-fill change is height-only; the drawer's width still clamps to
    min(460px, 92vw) so a narrow/mobile viewport renders correctly."""
    assert "width:min(460px,92vw)" in BOARD_PAGE


# ── "saying" renders markdown (bd-p87t) ──────────────────────────────────────────
#
# The coder's answer_tail is markdown prose. The "saying" section now renders it through
# a lazily-CDN-loaded markdown pass (marked) instead of showing raw esc()'d syntax, with
# a plain-text fallback if the CDN load fails and a hard XSS guard. "thinking" stays plain
# escaped text — internal reasoning, not user-facing prose.


def test_saying_carries_raw_markdown_and_a_plain_text_fallback():
    """The saying div keeps the .thought class (so it inherits the drawer scroll/overflow
    cap + the lone-gen fill), adds the .md-saying enhancement hook + a data-md attribute
    holding the raw markdown, and renders esc()'d text inline as the fallback until the
    renderer upgrades it — so answer_tail is esc()'d in BOTH the attribute and the body."""
    assert 'class="thought md-saying" data-md="' in BOARD_PAGE
    # esc()'d in the data-md attribute AND as the inline fallback body → two sites.
    assert BOARD_PAGE.count("esc(g.answer_tail)") == 2
    # the old raw-text saying render (plain .thought straight off the "saying" label) is gone.
    assert '>saying</div><div class="thought">' not in BOARD_PAGE


def test_thinking_section_stays_plain_escaped_text_not_markdown():
    """thought_tail (internal reasoning) is NOT markdown-rendered — it keeps the plain
    .thought div and plain esc()'d text, with no md-saying hook."""
    assert 'thinking</div><div class="thought">\'+esc(g.thought_tail)' in BOARD_PAGE


def test_markdown_renderer_loads_lazily_from_cdn_with_a_fallback():
    """The renderer (marked) loads once, lazily, from cdnjs on first drawer open; a CDN
    failure resolves to null so the section keeps its plain esc()'d fallback (no throw)."""
    assert 'const MARKED_CDN = "https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js";' in BOARD_PAGE
    assert "function loadMarked()" in BOARD_PAGE
    assert "s.async = true;" in BOARD_PAGE  # lazy — never blocks initial paint
    assert "s.onerror = () => resolve(null);" in BOARD_PAGE  # CDN blocked/offline fallback
    assert "if (!marked) return;" in BOARD_PAGE  # apply is a no-op when the lib is unavailable
    # renderMonitor drives the upgrade after each (re-)render of the drawer body.
    assert 'enhanceSaying($("drawer-body"));' in BOARD_PAGE


def test_saying_markdown_is_xss_safe():
    """No raw HTML passthrough: the source's angle brackets are escaped BEFORE parsing so
    tags render as literal text, and the only URL-bearing attributes markdown can emit are
    scrubbed of dangerous schemes."""
    assert '.replace(/</g, "&lt;").replace(/>/g, "&gt;")' in BOARD_PAGE
    assert "function sanitizeSaying(el)" in BOARD_PAGE
    assert "javascript|data|vbscript" in BOARD_PAGE
    # breaks:true for the coder's newline handling, per the task.
    assert "{breaks: true, gfm: true}" in BOARD_PAGE


def test_saying_markdown_css_scales_down_and_uses_pl_tokens():
    """Rendered markdown is styled for the compact drawer: headings ~14px (not full-page
    size), code on a subtle --pl token background (distinct from the raised drawer bg),
    lists indented — and .md-on only flips whitespace once markdown is rendered, so the
    plain-text fallback keeps its pre-wrap layout."""
    assert ".gen .thought.md-on{white-space:normal}" in BOARD_PAGE
    assert ".gen .md-on h1,.gen .md-on h2{font-size:14px}" in BOARD_PAGE  # r3: scaled-down heading
    # r2: code block + inline code on a --pl token background (monospace).
    assert (
        ".gen .md-on code{font-family:var(--pl-font-mono);font-size:10.5px;background:var(--pl-color-bg);" in BOARD_PAGE
    )
    assert ".gen .md-on pre{background:var(--pl-color-bg);" in BOARD_PAGE
    # lists indent properly.
    assert ".gen .md-on ul,.gen .md-on ol{margin:4px 0;padding-left:18px}" in BOARD_PAGE


# ── safe board hrefs + pinned marked CDN (S2/F3) ─────────────────────────────────
#
# The external-ref slot (projected as pr_url) renders as a live footer link, so the
# view gates it a second time behind the store's persistence check: safeHref lets only
# an absolute http(s) URL through and collapses everything else to the inert "#". The
# lazily-loaded markdown renderer is pinned by subresource integrity so only the exact
# 12.0.2 artifact ever executes.


def test_safehref_allows_only_absolute_http_s_urls_and_falls_back_to_inert_hash():
    """safeHref is the render-side half of the external-ref gate (the store's
    normalize_external_ref is the persistence half): only a parsed absolute http(s)
    URL goes live; any other scheme — and any string that isn't a URL at all — comes
    back as the inert "#", the same scheme discipline sanitizeSaying applies to
    markdown links."""
    assert "function safeHref(u)" in BOARD_PAGE
    assert "const p = new URL(String(u));" in BOARD_PAGE  # no base URL — relative refs stay inert
    assert 'if (p.protocol === "http:" || p.protocol === "https:") return p.href;' in BOARD_PAGE
    assert 'return "#";' in BOARD_PAGE


def test_every_pr_url_href_is_minted_through_safehref():
    """The rendered anchor HTML for BOTH footer links (coding 'PR ↗' + task 'ref ↗')
    routes pr_url through safeHref before esc() — so the emitted href attribute can
    never carry another scheme — and opens with rel noopener/noreferrer. No render
    site interpolates the raw pr_url into an href."""
    assert (
        '\'<a class="pr" href="\'+esc(safeHref(f.pr_url))+\'" target="_blank" rel="noopener noreferrer">PR ↗</a>\''
        in BOARD_PAGE
    )
    assert (
        '\'<a class="pr" href="\'+esc(safeHref(f.pr_url))+\'" target="_blank" rel="noopener noreferrer">ref ↗</a>\''
        in BOARD_PAGE
    )
    # the old unguarded interpolation is gone — pr_url reaches an href ONLY via safeHref
    assert "href=\"'+esc(f.pr_url)" not in BOARD_PAGE
    # exactly two dynamic href sites exist in the page, and both are the guarded ones
    assert BOARD_PAGE.count("href=\"'+") == 2
    assert BOARD_PAGE.count("href=\"'+esc(safeHref(") == 2


# ── preflight-held projects render as a warning card (#261) ──────────────────────
#
# /status carries `held_projects` (sorted names) + `preflight.held` ({project: reason})
# — the projects whose ready cards the loop froze behind a gate that fails on the clean
# base (#255). The page used to drop them on the floor: a fully-`ready` board with every
# card held just looked idle. They now render as a warning card alongside the setup gap.


def test_held_projects_have_a_render_path_that_names_each_reason():
    """heldGateItems builds one <li> per held project from held_projects + the
    preflight.held reason map, esc()'d on both halves — server text, never raw HTML."""
    assert "function heldGateItems(s)" in BOARD_PAGE
    assert "(s && s.preflight && s.preflight.held) || {};" in BOARD_PAGE
    assert "(s && s.held_projects) || [];" in BOARD_PAGE
    assert "esc(name)" in BOARD_PAGE
    assert 'esc(String(held[name] || "gate preflight failed"))' in BOARD_PAGE


def test_held_projects_get_their_own_warning_card_on_an_otherwise_healthy_board():
    """When setup is fine, held projects render their own pl-callout--warning card —
    checked BEFORE the loop-stale info callout (a frozen project outranks a restart
    note) — and the card names the self-healing recovery, matching setupLoopLine's
    'no restart needed' language."""
    assert "function renderHeldProjects(s)" in BOARD_PAGE
    assert "else if (s && s.held_projects && s.held_projects.length) renderHeldProjects(s);" in BOARD_PAGE
    # held check sits between the setup-gap branch and the loop-stale branch
    ready_branch = BOARD_PAGE.index("renderSetupGaps(s.setup, null, s);")
    held_branch = BOARD_PAGE.index("renderHeldProjects(s);")
    stale_branch = BOARD_PAGE.index("renderLoopStale(s.setup);")
    assert ready_branch < held_branch < stale_branch
    assert "releases its cards on its own once the gate passes (no restart needed)" in BOARD_PAGE


def test_held_card_still_shows_the_stale_loop_and_paused_advisories():
    """The held branch OUTRANKS the loop-stale branch, so it has to carry that branch's
    advisories itself — otherwise a board that is both held and running a stale config
    shows only the hold, and its "releases on its own (no restart needed)" line is
    actively wrong: the restart is what applies the config the operator just changed.

    RED-IS-REACHABLE: drop `setupLoopLine` from renderHeldProjects and this fails
    (found by the retroactive adversarial review of #273, which merged without one)."""
    body = BOARD_PAGE[BOARD_PAGE.index("function renderHeldProjects(s)") :]
    body = body[: body.index("\n}")]
    assert "setupLoopLine(" in body, "the held card must carry the advisories it outranks"
    # the same source both other cards read it from — no second copy to drift
    assert BOARD_PAGE.count("function setupLoopLine(setup)") == 1


def test_held_projects_ride_along_the_setup_gap_card_when_both_fail():
    """A board with a setup gap AND held projects shows both on the one card slot —
    both /status call sites hand the full status body through to renderSetupGaps,
    which appends the held list under its own heading (omitted when empty)."""
    assert "function renderSetupGaps(setup, e, s)" in BOARD_PAGE
    assert "renderSetupGaps(s.setup, null, s);" in BOARD_PAGE  # load() success path
    assert "renderSetupGaps(s.setup, e, s); return;" in BOARD_PAGE  # read-error fallback path
    assert "const held = heldGateItems(s);" in BOARD_PAGE
    assert "<b>Held projects (gate fails on clean base):</b>" in BOARD_PAGE


def test_marked_cdn_script_is_pinned_with_integrity_and_crossorigin():
    """The lazily-injected marked script carries a subresource-integrity pin for the
    12.0.2 artifact plus crossorigin=anonymous (required for SRI enforcement on a
    cross-origin script), so a response that doesn't match the pinned digest never
    executes — it fails closed into the plain esc()'d fallback via s.onerror."""
    assert (
        'const MARKED_SRI = "sha512-xeUh+KxNyTufZOje++oQHstlMQ8/rpyzPuM+gjMFYK3z5ILJGE7l2NvYL+XfliKURMpBIKKp1XoPN/qswlSMFA==";'
        in BOARD_PAGE
    )
    assert "s.integrity = MARKED_SRI;" in BOARD_PAGE
    assert 's.crossOrigin = "anonymous";' in BOARD_PAGE


# ── task detail moves into the #drawer (bd-rdmh) ─────────────────────────────────
#
# Task detail + controls leave the inline Kanban card / list sub-row and move into the
# existing #drawer. The card keeps only summary content (title, id, priority, doc glyph,
# state chips) and becomes clickable; the drawer is card-type aware ("Task — <id>") and
# shows spec / acceptance criteria / any recorded deliverable + the state-appropriate
# controls. Monitor polling stays coding-only (a task has no coder run). These are
# structural assertions on the page source — the suite has no JS runtime (see the module
# docstring), so a "rendered-HTML" check pins the markup the card/drawer paths emit.


def test_r1_task_cards_render_no_inline_form_or_action_controls():
    """r1: a task card in ANY column renders only summary content — the inline task-form
    markup (textarea / url input / action buttons) is gone from BOTH projections. The
    Kanban card no longer appends taskExtra, and the list's task sub-row is removed, so
    taskExtra is reachable ONLY from taskDetail (the drawer)."""
    # the old inline card append + the old list sub-row are both gone
    assert '(isTask(f)?taskExtra(f):"")' not in BOARD_PAGE
    assert 'class="tsub"' not in BOARD_PAGE
    assert ".tsub>td" not in BOARD_PAGE  # its dead CSS rule is removed too
    # taskExtra now has exactly two sites: its definition + the one taskDetail call
    assert BOARD_PAGE.count("taskExtra(f)") == 2
    # the Kanban card closes summary-only (title row + meta row, no task extra after it)
    assert "+ flags(f)+' '+taskFoot(f)+'</div></div>';" in BOARD_PAGE


def test_r1_r2_task_cards_and_rows_are_clickable_via_a_data_task_handle():
    """r1/r2: every task card (Kanban) and row (list) carries a data-task handle so a
    delegated click opens the drawer — two sites, one per projection — and both use the
    same isTask ternary that stamps the handle only onto task cards/rows."""
    assert BOARD_PAGE.count("data-task=\"'+esc(f.id)+'\"") == 2
    assert BOARD_PAGE.count("(isTask(f)?' data-task=\"'+esc(f.id)+'\"':\"\")") == 2
    # clickable cursor + hover cue extends to task cards/rows, not only the monitor ones
    assert ".card--live,.card--task{cursor:pointer}" in BOARD_PAGE
    assert ".card--live:hover,.card--task:hover{border-color:var(--pl-color-accent)}" in BOARD_PAGE
    assert "#list tr[data-mon],#list tr[data-task]{cursor:pointer}" in BOARD_PAGE


def test_r2_task_drawer_is_card_type_aware_and_shows_spec_ac_and_deliverable():
    """r2: clicking a task opens the shared drawer titled "Task — <id>" and renders the
    task's spec, acceptance criteria, and any recorded deliverable (taskDetail), then the
    state-appropriate controls (taskExtra)."""
    assert "function openTask(fid)" in BOARD_PAGE
    assert '$("drawer-title").textContent = "Task — " + fid;' in BOARD_PAGE
    assert "function taskDetail(f)" in BOARD_PAGE
    assert "'<div class=\"tdlbl\">spec</div>' + prose(f.spec)" in BOARD_PAGE
    assert "'<div class=\"tdlbl\">acceptance criteria</div>' + prose(f.acceptance_criteria)" in BOARD_PAGE
    assert (
        "if (f.deliverable) h += '<div class=\"tdlbl\">deliverable</div><div class=\"deliv\">'+esc(f.deliverable)+'</div>';"
        in BOARD_PAGE
    )
    assert "return h + taskExtra(f);" in BOARD_PAGE
    # the deliverable is esc()'d exactly once (in the drawer), not on the card
    assert BOARD_PAGE.count("esc(f.deliverable)") == 1


def test_r2_task_click_is_delegated_and_the_actions_still_route_through_the_routes():
    """r2: the delegated click listener opens the task drawer on [data-task] (alongside
    the monitor's [data-mon]); the deliver/approve/reject verbs are unchanged, so they
    still POST /deliver and /verify from the drawer through the existing routes."""
    assert 'e.target.closest("[data-task]")' in BOARD_PAGE
    assert 'openTask(tel.getAttribute("data-task"))' in BOARD_PAGE
    # the mutation routes are untouched — the controls just moved into the drawer body
    assert '"/deliver", {text: text, ref: ref}' in BOARD_PAGE
    assert '"/verify", {approved: true}' in BOARD_PAGE
    assert '"/verify", {approved: false, feedback: feedback}' in BOARD_PAGE


def test_r2_open_task_drawer_tracks_state_across_re_renders():
    """r2: the open task drawer re-renders from the live FEATURES after every render()
    — a reject-toggle, a mutation's reload, or the 10s auto-reload — so its state
    (in_progress → in_review → done) and the reject-form toggle survive a re-render.
    syncTaskDrawer is guarded on TASK_FID (no-op when a task isn't open) and is called
    from render() and openTask (two call sites, plus its definition)."""
    assert "function syncTaskDrawer()" in BOARD_PAGE
    assert "if (!TASK_FID) return;" in BOARD_PAGE
    assert "const f = FEATURES.find(x => x.id === TASK_FID);" in BOARD_PAGE
    assert BOARD_PAGE.count("syncTaskDrawer();") == 2  # render()'s tail + openTask


def test_r3_monitor_stays_coding_only_and_tasks_never_poll():
    """r3: the monitor gate still excludes tasks in BOTH projections, and the monitor
    still polls; a task opens the SAME drawer but with NO interval — openTask clears any
    monitor timer and never starts one (a task has no coder run)."""
    assert "const mon = live && !isTask(f);" in BOARD_PAGE  # kanban gate
    assert 'const mon = f.state==="in_progress" && !isTask(f);' in BOARD_PAGE  # list gate
    assert "setInterval(pollMonitor, MON_POLL_MS)" in BOARD_PAGE  # coding still polls
    # openTask stops polling (MON_FID=null fences an in-flight poll) and starts none
    assert (
        "MON_FID = null;\n  if (MON_TIMER) { clearInterval(MON_TIMER); MON_TIMER = null; }\n  TASK_FID = fid;"
        in BOARD_PAGE
    )
    # only two setInterval sites exist — the monitor poll + the 10s board reload; openTask
    # adds no third (tasks don't poll)
    assert BOARD_PAGE.count("setInterval(") == 2


def test_r3_pollmonitor_fences_a_late_poll_from_clobbering_a_repurposed_drawer():
    """r3 (the blocking review finding): clearInterval stops FUTURE polls but cannot
    cancel a request already awaited by pollMonitor, so an in-flight poll that resolves
    (or fails) AFTER the drawer is switched to a task must not overwrite the task detail.
    pollMonitor captures the fid it started with and re-checks MON_FID before writing on
    BOTH the success and the error path — and the error write is guarded by the fence,
    the exact clobber the review caught."""
    assert "const fid = MON_FID;" in BOARD_PAGE
    assert BOARD_PAGE.count("if (MON_FID !== fid) return;") == 2  # success + catch both fenced
    # the catch's error write only runs AFTER the fence — it can't clobber a task drawer
    assert (
        'if (MON_FID !== fid) return;\n    $("drawer-body").innerHTML = '
        "'<div class=\"pl-callout pl-callout--error\">'+esc(\"\"+e)+'</div>';" in BOARD_PAGE
    )


def test_r4_drawer_close_behavior_is_preserved_and_clears_both_modes():
    """r4: Esc + the scrim still close the shared drawer, and it keeps role="dialog".
    The single close routine (button / scrim / Esc all route through closeMonitor) now
    clears BOTH drawer modes, so a task drawer closes cleanly too."""
    assert 'role="dialog"' in BOARD_PAGE
    assert 'e.key === "Escape"' in BOARD_PAGE
    assert '$("scrim").addEventListener("click", closeMonitor)' in BOARD_PAGE
    assert '$("drawer-close").addEventListener("click", closeMonitor)' in BOARD_PAGE
    assert "MON_FID = null; TASK_FID = null;" in BOARD_PAGE  # close clears both modes
