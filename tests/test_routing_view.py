"""Configure ▸ Coder routing (#578): a settings tab, not a board tab."""

from pathlib import Path

from project_board.board_view import BOARD_PAGE
from project_board.routing_view import ROUTING_PAGE

VIEW = Path(__file__).resolve().parent.parent / "view"


def test_routing_page_assembles_all_source_files():
    for name in ("routing.html", "routing.css", "routing.js"):
        assert (VIEW / name).is_file()
    assert "__ROUTING_CSS__" not in ROUTING_PAGE and "__ROUTING_JS__" not in ROUTING_PAGE
    assert (VIEW / "routing.js").read_text(encoding="utf-8") in ROUTING_PAGE


def test_routing_page_waits_for_the_host_bearer_like_projects():
    # The Configure frame hands over the bearer in the init handshake; a timer that fetched
    # before it would 401 on a fleet member (the first-fetch-races-bearer trap).
    assert 'import(BASE + "/_ds/plugin-kit.js")' in ROUTING_PAGE
    assert "kit.initPluginView" in ROUTING_PAGE and "kit.apiFetch" in ROUTING_PAGE
    assert "setTimeout(() => { if (!booted)" not in ROUTING_PAGE
    assert '"/api/plugins/project_board/routing/effective"' in ROUTING_PAGE


def test_switch_saves_and_every_coder_on_clears_the_exclusive():
    assert 'addEventListener("change"' in ROUTING_PAGE
    assert "routingSave(on.length === all.length ? [] : on)" in ROUTING_PAGE
    assert "At least one coder has to build." in ROUTING_PAGE


def test_board_page_has_no_routing_tab():
    assert "setView('routing')" not in BOARD_PAGE
    assert 'id="routing"' not in BOARD_PAGE
