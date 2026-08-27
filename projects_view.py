"""Sandboxed Configure-tab page for the live project registry."""

from __future__ import annotations

from pathlib import Path

_VIEW_DIR = Path(__file__).resolve().parent / "view"


def _assemble_projects_page() -> str:
    html = (_VIEW_DIR / "projects.html").read_text(encoding="utf-8")
    css = (_VIEW_DIR / "projects.css").read_text(encoding="utf-8")
    js = (_VIEW_DIR / "projects.js").read_text(encoding="utf-8")
    return html.replace("__PROJECTS_CSS__", css).replace("__PROJECTS_JS__", js)


PROJECTS_PAGE = _assemble_projects_page()
