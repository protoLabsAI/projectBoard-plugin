"""Sandboxed Configure-tab page for coder routing (#578)."""

from __future__ import annotations

from pathlib import Path

_VIEW_DIR = Path(__file__).resolve().parent / "view"


def _assemble_routing_page() -> str:
    html = (_VIEW_DIR / "routing.html").read_text(encoding="utf-8")
    css = (_VIEW_DIR / "routing.css").read_text(encoding="utf-8")
    js = (_VIEW_DIR / "routing.js").read_text(encoding="utf-8")
    return html.replace("__ROUTING_CSS__", css).replace("__ROUTING_JS__", js)


ROUTING_PAGE = _assemble_routing_page()
