"""Overview Configure tab (S1a, #611) — the first Settings tab, answering "is it working?".

Served at ``/plugins/project_board/config/overview`` by the public router in api.py, the
same way as ``/config/projects`` and ``/config/routing`` (ADR 0026: public page chrome, no
data in the page; every read/write rides the operator-bearer-gated ``/api`` prefix via the
DS plugin-kit's authed fetch). The page holds the board on/off switch (``loop_enabled``,
saved through PUT /settings), live counts of building / in review / blocked / waiting cards,
a Problems list with a fix for each, and a pending-restart banner.

Unlike the board/projects/routing views (three source files each), this page is ONE file —
``view/overview.html`` — with its markup, a ``<style>`` block and a ``<script type="module">``
inline, so the card stays within breadth. This module just reads that file at import time
(``pathlib`` relative to ``__file__``, the same drop-in, no-build pattern) and exposes it as
``OVERVIEW_PAGE``; api.py serves it on GET /config/overview with ``Cache-Control: no-store``.
"""

from __future__ import annotations

from pathlib import Path

_VIEW_DIR = Path(__file__).resolve().parent / "view"


def _assemble_overview_page() -> str:
    """Read the single ``view/overview.html`` source file. It already carries its ``<style>``
    and ``<script type="module">`` inline, so there is nothing to splice — one GET, no extra
    asset fetches, which is what keeps the fleet-proxy base derivation intact."""
    return (_VIEW_DIR / "overview.html").read_text(encoding="utf-8")


OVERVIEW_PAGE = _assemble_overview_page()
