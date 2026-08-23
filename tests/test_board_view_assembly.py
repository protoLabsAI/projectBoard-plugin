"""Board-view extraction contract (#239).

The ~400-line inline ``BOARD_PAGE`` raw string was extracted into three proper
source files under ``view/`` — ``board.html`` (skeleton + placeholders),
``board.css`` (the ``var(--pl-*)`` stylesheet), and ``board.js`` (the ESM module) —
so standard tooling (eslint / prettier / stylelint / htmlhint) can lint, parse, and
format them. ``board_view.py`` now reads the three files at import time and inlines
the CSS into the ``<style>`` block and the JS into the ``<script type="module">``
block. These tests pin that refactor: the files exist, their contents are inlined
verbatim, the page stays a single-response SPA (no extra asset fetches), and the
fleet-proxy (ADR 0042) + handshake (ADR 0026) contracts survive the move.
"""

from __future__ import annotations

from pathlib import Path

from project_board.board_view import BOARD_PAGE

ROOT = Path(__file__).resolve().parent.parent
VIEW = ROOT / "view"


# ── r1: the three source files exist under view/ ─────────────────────────────────


def test_view_source_files_exist_as_separate_files():
    for name in ("board.html", "board.css", "board.js"):
        p = VIEW / name
        assert p.is_file(), f"missing extracted source file {name}"
        assert p.read_text(encoding="utf-8").strip(), f"{name} is empty"


# ── r2/r3: board_view.py inlines the CSS + JS verbatim into the assembled page ────


def test_css_is_inlined_verbatim_into_a_style_block():
    css = (VIEW / "board.css").read_text(encoding="utf-8")
    # The stylesheet is inlined byte-for-byte between the <style> tags — no external
    # <link> to board.css (single GET; keeps the fleet-proxy base derivation simple).
    assert "<style>\n" + css + "</style>" in BOARD_PAGE
    # a couple of load-bearing rules made the trip
    assert "#drawer .db{" in css
    assert "var(--pl-color-bg)" in css


def test_js_is_inlined_verbatim_into_a_module_script_block():
    js = (VIEW / "board.js").read_text(encoding="utf-8")
    assert '<script type="module">\n' + js + "</script></body></html>" in BOARD_PAGE
    # the ESM module body actually came across
    assert "kit.initPluginView(boot)" in js
    assert "async function load()" in js


def test_html_skeleton_uses_placeholders_that_are_fully_substituted():
    html = (VIEW / "board.html").read_text(encoding="utf-8")
    # the template carries exactly one slot for each asset…
    assert html.count("__BOARD_CSS__") == 1
    assert html.count("__BOARD_JS__") == 1
    # …and the assembled page has none left over (every placeholder was filled).
    assert "__BOARD_CSS__" not in BOARD_PAGE
    assert "__BOARD_JS__" not in BOARD_PAGE


# ── r3: the assembled page is still a single-response SPA ─────────────────────────


def test_page_is_single_response_no_extra_asset_fetches_for_the_extracted_files():
    """The extraction must not turn the one-GET SPA into a multi-fetch page — the
    iframe still loads exactly one document, so nothing references the extracted
    files by URL (the only same-origin assets are the DS kit, which was always so)."""
    assert "board.css" not in BOARD_PAGE  # no <link href=…board.css>
    assert "board.js" not in BOARD_PAGE  # no <script src=…board.js>
    # exactly one stylesheet block and one module script — the inlined ones.
    assert BOARD_PAGE.count("<style>") == 1
    assert BOARD_PAGE.count('<script type="module">') == 1


def test_assembled_page_is_a_full_html_document():
    assert BOARD_PAGE.lstrip().lower().startswith("<!doctype html>")
    assert BOARD_PAGE.rstrip().endswith("</html>")


# ── r5/r6: the ADR 0042 (fleet-proxy) + ADR 0026 (handshake) contracts survive ───


def test_fleet_proxy_base_derivation_survives_extraction():
    """ADR 0042: base is derived from the page's own path (→ "" on host,
    "/agents/<slug>" when proxied); the derivation now lives in board.html's inline
    bootstrap and must reach the assembled page unchanged."""
    assert 'var BASE = location.pathname.split("/plugins/")[0];' in BOARD_PAGE
    assert 'l.href=BASE+"/_ds/plugin-kit.css"' in BOARD_PAGE


def test_plugin_view_handshake_survives_extraction():
    """ADR 0026: the DS kit owns the postMessage bearer + theme handshake; the page
    still boots through it and fetches via its slug-aware authed fetch."""
    assert 'import(BASE + "/_ds/plugin-kit.js")' in BOARD_PAGE  # ESM dynamic import
    assert "kit.initPluginView(boot)" in BOARD_PAGE
    assert "kit.apiFetch" in BOARD_PAGE
    # no hand-rolled theme :root map / message listener crept back in
    assert ":root{" not in BOARD_PAGE and ":root {" not in BOARD_PAGE
    assert 'addEventListener("message"' not in BOARD_PAGE


# ── r10: the view/ assets are packaged (MANIFEST.in) ─────────────────────────────


def test_manifest_in_packages_the_view_assets():
    """No build step — but if an sdist is ever built, the non-.py view assets must
    ride along or the drop-in plugin can't render /board."""
    manifest = ROOT / "MANIFEST.in"
    assert manifest.is_file(), "MANIFEST.in is missing"
    text = manifest.read_text(encoding="utf-8")
    assert "recursive-include view" in text
    for ext in ("*.html", "*.css", "*.js"):
        assert ext in text, f"MANIFEST.in does not include {ext}"
