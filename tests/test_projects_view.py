"""Configure-tab Projects view contract (S2 #612: compact rows + side-panel editor)."""

from pathlib import Path

from project_board.projects_view import PROJECTS_PAGE

ROOT = Path(__file__).resolve().parent.parent
VIEW = ROOT / "view"


def test_projects_page_assembles_all_source_files():
    for name in ("projects.html", "projects.css", "projects.js"):
        assert (VIEW / name).is_file()
    assert "__PROJECTS_CSS__" not in PROJECTS_PAGE
    assert "__PROJECTS_JS__" not in PROJECTS_PAGE
    assert (VIEW / "projects.css").read_text(encoding="utf-8") in PROJECTS_PAGE
    assert (VIEW / "projects.js").read_text(encoding="utf-8") in PROJECTS_PAGE


def test_projects_page_uses_the_fleet_safe_authenticated_plugin_kit():
    assert 'location.pathname.split("/plugins/")[0]' in PROJECTS_PAGE
    assert 'import(BASE + "/_ds/plugin-kit.js")' in PROJECTS_PAGE
    assert "kit.initPluginView" in PROJECTS_PAGE
    assert "kit.apiFetch" in PROJECTS_PAGE
    assert 'const API = "/api/plugins/project_board/projects"' in PROJECTS_PAGE
    assert 'addEventListener("message"' not in PROJECTS_PAGE


def test_projects_page_loads_only_after_init_and_never_falls_back_to_a_timer():
    # The one boot path is initPluginView's callback — no setTimeout fallback that would load
    # (or poll /status) without the authenticated bridge being ready.
    assert "kit.initPluginView(() =>" in PROJECTS_PAGE
    assert "setTimeout(boot" not in PROJECTS_PAGE
    assert "setTimeout(load" not in PROJECTS_PAGE
    # The only legitimate timer is the save gate-wait poll, matched by request id.
    assert "await new Promise((resolve) => setTimeout(resolve, SAVE_POLL_MS))" in PROJECTS_PAGE


def test_projects_page_reads_the_live_preflight_from_status_for_the_checks_dot():
    assert 'const STATUS = "/api/plugins/project_board/status"' in PROJECTS_PAGE
    # Both reads go through the authed kit fetch, in parallel.
    assert 'request(API, {cache:"no-store"})' in PROJECTS_PAGE
    assert 'request(STATUS, {cache:"no-store"})' in PROJECTS_PAGE
    assert "Promise.all(" in PROJECTS_PAGE
    # Dot states: held -> error (with its one-line reason), in the per-project `passed` set ->
    # success, anything else -> neutral "not run".
    assert "function checksFrom(status)" in PROJECTS_PAGE
    assert "status.preflight" in PROJECTS_PAGE
    assert "pl-dot--error" in PROJECTS_PAGE and "pl-dot--success" in PROJECTS_PAGE
    assert "checks.failures?.[name]" in PROJECTS_PAGE
    assert "checks.held[name]" in PROJECTS_PAGE
    # A status read failure must not fail the page — the list is primary, dots best-effort.
    assert 'request(STATUS, {cache:"no-store"}).catch(() => null)' in PROJECTS_PAGE


def test_projects_green_dot_reads_a_per_project_pass_not_a_board_wide_flag():
    """#612 review (blocking): green came from one board-wide `evaluated` flag — once any
    preflight map had an entry, every project not in `held` went green, including the
    `slow` (indeterminate), `dirty` and `unwinnable_oracle` entries that SET the flag and
    any project added after the last pass. That is a false green. Green must instead read
    the ONE authoritative per-project verdict the loop publishes: `preflight.passed`."""
    # The dot greens ONLY on a positive per-project pass, and that set comes straight from
    # the published preflight — no board-wide "anything ran" flag survives.
    assert "checks.passed.has(name)" in PROJECTS_PAGE
    assert "passed: new Set(Array.isArray(pf.passed) ? pf.passed : [])" in PROJECTS_PAGE
    assert "evaluated" not in PROJECTS_PAGE


def test_projects_rows_are_compact_and_never_show_the_gate_command():
    # Compact one-line rows with a checks dot; the old card/meta layout is gone.
    assert 'class="prow"' in PROJECTS_PAGE
    assert 'class="rows"' in PROJECTS_PAGE
    assert '<span class="pl-dot ' in PROJECTS_PAGE
    assert "Local gate</dt>" not in PROJECTS_PAGE
    assert '<dl class="meta"' not in PROJECTS_PAGE
    # Short repo path with the home dir shown as `~`.
    assert "function shortRepo(repo)" in PROJECTS_PAGE
    assert '"~"' in PROJECTS_PAGE


def test_projects_failing_dot_carries_its_one_line_reason_without_opening_the_row():
    assert 'class="prow-reason"' in PROJECTS_PAGE
    # headline (redacted one-liner) preferred, then the held reason string.
    assert "failure?.headline" in PROJECTS_PAGE
    assert 'variant:"pl-dot--error"' in PROJECTS_PAGE


def test_projects_editor_is_a_side_panel_with_a_collapsed_monospace_checks_section():
    assert 'class="side-panel"' in PROJECTS_PAGE
    assert 'role="dialog"' in PROJECTS_PAGE
    assert 'id="backdrop"' in PROJECTS_PAGE
    # Gate command + conventions live under a collapsed <details> Checks section.
    assert '<details id="checks" class="checks">' in PROJECTS_PAGE
    assert "<summary>Checks</summary>" in PROJECTS_PAGE
    assert '$("checks").open = false' in PROJECTS_PAGE
    # Monospace and wrapped.
    assert ".mono{font-family:var(--pl-font-mono)}" in PROJECTS_PAGE
    assert ".checks .mono{white-space:pre-wrap" in PROJECTS_PAGE
    assert 'id="gate" name="local_gate_cmd" class="mono"' in PROJECTS_PAGE


def test_projects_page_has_explicit_failure_retry_busy_and_delete_confirmation_states():
    assert "[hidden]{display:none!important}" in PROJECTS_PAGE
    assert 'id="retry"' in PROJECTS_PAGE
    assert 'role="alert"' in PROJECTS_PAGE
    assert 'setAttribute("aria-busy"' in PROJECTS_PAGE
    assert "window.confirm" in PROJECTS_PAGE
    assert "Deleting…" in PROJECTS_PAGE and "Saving…" in PROJECTS_PAGE
    assert 'id="add" class="btn primary" type="button" disabled' in PROJECTS_PAGE
    assert 'id="editor-fields"' in PROJECTS_PAGE
    assert 'event.key === "Escape"' in PROJECTS_PAGE
    assert 'id="page-title" tabindex="-1"' in PROJECTS_PAGE


def test_projects_page_keeps_todays_save_semantics_409_and_gate_wait():
    # A new gate / moved repo runs the gate once; the proxy may 504 first, so the outcome
    # is read back by request id. A concurrent save answers 409. None of this changed.
    assert "request_id:requestId" in PROJECTS_PAGE
    assert '"Running the gate…"' in PROJECTS_PAGE
    assert "runs its gate once on the clean base before anything is saved" in PROJECTS_PAGE
    assert "error.status === 502 || error.status === 504" in PROJECTS_PAGE
    assert "data.saves?.[name]" in PROJECTS_PAGE and "save.id !== requestId" in PROJECTS_PAGE
    # Refusals/gate-wait render inside the open panel, not behind it.
    assert "function editorMessage(text, error=false)" in PROJECTS_PAGE


def test_projects_page_uses_strict_default_intent_and_no_unauthenticated_fetch_fallback():
    assert 'defaultAction = $("default").checked ? "set"' in PROJECTS_PAGE
    assert "default_action:defaultAction" in PROJECTS_PAGE
    assert "apiFetch:(p,i)=>fetch" not in PROJECTS_PAGE
    assert "Could not load the authenticated plugin bridge" in PROJECTS_PAGE
    assert "state.projects.length === 0" in PROJECTS_PAGE
    assert "state.projects.length === 1" in PROJECTS_PAGE
    assert "The only project is always the default." in PROJECTS_PAGE


def test_projects_page_protects_malformed_entries_and_formats_api_validation_errors():
    assert "project.editable !== false" in PROJECTS_PAGE
    assert "the editor will not overwrite it" in PROJECTS_PAGE
    assert "Array.isArray(data.detail)" in PROJECTS_PAGE


def test_projects_page_distinguishes_applied_mutations_from_refresh_failures():
    assert "The change applied; Retry to reload live state." in PROJECTS_PAGE
    assert "state.projects = state.projects.filter" in PROJECTS_PAGE
    assert 'button.textContent = "Delete"' in PROJECTS_PAGE
    assert 'removeAttribute("aria-busy")' in PROJECTS_PAGE


def test_projects_page_gates_editing_on_a_usable_onboarding_space():
    """Editing needs a root, not just the switch (protoAgent#3396).

    The host refuses a write whose repo can't resolve under `onboarding.root`, so a
    button gated on `enabled` alone goes live the moment onboarding defaults on and then
    fails on submit. The warning appears ONLY when adding is actually blocked and names the
    ONE missing piece and where to set it, rather than restating both bounds whichever is unmet.
    Delete stays available so unused entries can still be removed."""
    assert "function canEdit(){ return state.onboarding.enabled && Boolean(state.onboarding.root); }" in PROJECTS_PAGE
    assert '$("add").disabled = busy || !canEdit();' in PROJECTS_PAGE
    assert '!canEdit() || button.dataset.editable === "false"' in PROJECTS_PAGE
    assert "onboard.hidden = canEdit();" in PROJECTS_PAGE
    assert "needs an onboarding root" in PROJECTS_PAGE
    assert "needs Project onboarding switched on" in PROJECTS_PAGE
    assert "Settings ▸ Capabilities ▸ Project onboarding" in PROJECTS_PAGE


def test_projects_css_uses_design_system_tokens_and_no_hardcoded_colours():
    css = (VIEW / "projects.css").read_text(encoding="utf-8")
    # No id selectors are used, so any `#` would be a hex colour literal — there must be none.
    assert "#" not in css
    assert "rgb(" not in css and "rgba(" not in css and "hsl(" not in css
    # Colours come from DS tokens (works in light and dark).
    assert "var(--pl-color-bg)" in css and "var(--pl-color-fg)" in css
    assert "var(--pl-color-status-error)" in css
