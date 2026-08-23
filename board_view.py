"""Board console view (ADR 0026, D5) — the Kanban/list board UI.

A self-contained page served at ``/plugins/project_board/board`` (by the API
router in api.py — see __init__.register) that renders the board two ways (Kanban
columns = the 6 states, and a dense list), toggled — two projections of the same
features over the already-proven ``/features`` API. The console renders a left-rail
icon (manifest ``views:``) whose panel iframes this page; on load the console
``postMessage``s a bearer token + theme tokens (the ADR 0026 handshake), which the
page applies for its same-origin API calls.

FLEET-PROXY-SAFE (ADR 0042): the iframe loads at /plugins/project_board/board on
the host window, but at /agents/<slug>/plugins/project_board/board when this
agent is viewed through the fleet proxy. So the page derives ``base`` from its own
path (= "" on host, "/agents/<slug>" when proxied) and prefixes EVERY fetch + asset
with it — never hardcode an absolute "/api/...", "/plugins/...", or
"http://localhost:PORT" (that breaks the proxy + the same-origin postMessage token).

No build step — vanilla JS + inline SVG, so the whole plugin stays a drop-in
package. The page reads the board; it never mutates it (mutation stays the loop +
the tools + the API). ``BOARD_PAGE`` is the HTML; api.py serves it on GET /board.

The page lives as three proper source files under ``view/`` — ``board.html`` (the
document skeleton, with ``__BOARD_CSS__`` / ``__BOARD_JS__`` placeholders),
``board.css`` (the ``var(--pl-*)`` stylesheet), and ``board.js`` (the ESM module) —
so standard tooling (eslint / prettier / stylelint / htmlhint) can lint, parse, and
format them. There is still NO build step: this module reads the three files at
import time (``pathlib`` relative to ``__file__``) and inlines the CSS into the
``<style>`` block and the JS into the ``<script type="module">`` block, so the iframe
still loads the whole SPA in a SINGLE GET (no extra asset fetches — which is what
keeps the fleet-proxy base derivation and the postMessage handshake intact).
"""

from __future__ import annotations

from pathlib import Path

# The extracted source files ship next to this module (drop-in package — no build,
# no wheel-time asset step), so resolve them relative to __file__.
_VIEW_DIR = Path(__file__).resolve().parent / "view"


def _assemble_board_page() -> str:
    """Read the three ``view/`` source files and inline the CSS + JS into the HTML
    skeleton's placeholders — the assembly that used to be a hardcoded string. The
    result is a single self-contained document (one GET, no extra asset fetches),
    byte-for-byte the same page the inline ``BOARD_PAGE`` served."""
    html = (_VIEW_DIR / "board.html").read_text(encoding="utf-8")
    css = (_VIEW_DIR / "board.css").read_text(encoding="utf-8")
    js = (_VIEW_DIR / "board.js").read_text(encoding="utf-8")
    return html.replace("__BOARD_CSS__", css).replace("__BOARD_JS__", js)


BOARD_PAGE = _assemble_board_page()
