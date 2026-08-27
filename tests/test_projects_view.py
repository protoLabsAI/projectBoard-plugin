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
    assert "kit.initPluginView(boot)" in PROJECTS_PAGE
    assert "kit.apiFetch" in PROJECTS_PAGE
    assert 'const API = "/api/plugins/project_board/projects"' in PROJECTS_PAGE
    assert 'addEventListener("message"' not in PROJECTS_PAGE


def test_projects_page_has_explicit_failure_retry_busy_and_delete_confirmation_states():
    assert 'id="retry"' in PROJECTS_PAGE
    assert 'role="alert"' in PROJECTS_PAGE
    assert 'setAttribute("aria-busy", "true")' in PROJECTS_PAGE
    assert "window.confirm" in PROJECTS_PAGE
    assert "Deleting…" in PROJECTS_PAGE and "Saving…" in PROJECTS_PAGE
