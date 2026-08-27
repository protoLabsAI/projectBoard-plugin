"""Configure-tab Projects view contract."""

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


def test_projects_page_has_explicit_failure_retry_busy_and_delete_confirmation_states():
    assert 'id="retry"' in PROJECTS_PAGE
    assert 'role="alert"' in PROJECTS_PAGE
    assert 'setAttribute("aria-busy"' in PROJECTS_PAGE
    assert "window.confirm" in PROJECTS_PAGE
    assert "Deleting…" in PROJECTS_PAGE and "Saving…" in PROJECTS_PAGE
    assert 'id="add" class="btn primary" type="button" disabled' in PROJECTS_PAGE
    assert 'id="editor-fields"' in PROJECTS_PAGE
    assert 'event.key === "Escape"' in PROJECTS_PAGE


def test_projects_page_uses_strict_default_intent_and_no_unauthenticated_fetch_fallback():
    assert 'defaultAction = $("default").checked ? "set"' in PROJECTS_PAGE
    assert "default_action:defaultAction" in PROJECTS_PAGE
    assert "apiFetch:(p,i)=>fetch" not in PROJECTS_PAGE
    assert "Could not load the authenticated plugin bridge" in PROJECTS_PAGE
    assert "setTimeout(boot" not in PROJECTS_PAGE


def test_projects_page_protects_malformed_entries_and_formats_api_validation_errors():
    assert "project.editable !== false" in PROJECTS_PAGE
    assert "the editor will not overwrite it" in PROJECTS_PAGE
    assert "Array.isArray(data.detail)" in PROJECTS_PAGE


def test_projects_page_distinguishes_applied_mutations_from_refresh_failures():
    assert "The change applied; Retry to reload live state." in PROJECTS_PAGE
    assert "state.projects = state.projects.filter" in PROJECTS_PAGE
    assert 'button.textContent = "Delete"' in PROJECTS_PAGE
    assert 'removeAttribute("aria-busy")' in PROJECTS_PAGE
