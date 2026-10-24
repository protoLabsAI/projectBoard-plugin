"""Review & merge Configure tab (S4, #614) — the pipeline after a PR opens.

Served at ``/plugins/project_board/config/review`` by the public router in api.py, the same way
as ``/config/overview`` and ``/config/routing`` (ADR 0026: public page chrome, no data in the
page; every read/write rides the operator-bearer-gated ``/api`` prefix via the DS plugin-kit's
authed fetch). The page lays out the after-a-PR-opens pipeline as four ordered step cards —
Review → CI → Rebase → Merge — each an on/off ``pl-switch`` with its attempts as small steppers
(a step that is off greys out its sub-settings), plus the workflow / reviewer / merge-method
pickers and the dependent-cards radio. Every write goes through S0's guarded PUT /settings.

Like the Overview tab (S1a) this page is ONE file — ``view/review.html`` — with its markup, a
``<style>`` block and a ``<script type="module">`` inline, so the card stays within breadth.
This module reads that file at import time (``pathlib`` relative to ``__file__``, the same
drop-in, no-build pattern) and exposes it as ``REVIEW_PAGE``; api.py serves it on GET
/config/review with ``Cache-Control: no-store``.
"""

from __future__ import annotations

from pathlib import Path

_VIEW_DIR = Path(__file__).resolve().parent / "view"


def _assemble_review_page() -> str:
    """Read the single ``view/review.html`` source file. It already carries its ``<style>``
    and ``<script type="module">`` inline, so there is nothing to splice — one GET, no extra
    asset fetches, which is what keeps the fleet-proxy base derivation intact."""
    return (_VIEW_DIR / "review.html").read_text(encoding="utf-8")


REVIEW_PAGE = _assemble_review_page()
