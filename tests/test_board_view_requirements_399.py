"""#399 in the board view: the human verifier sees what is still open, before and after
approving.

The store and tools surface open requirements (``open_requirements`` on the card, a
``note`` on the verify result) — but a human verifying in the console reads the task
drawer and clicks Approve, and that path showed neither: the drawer rendered no ledger,
and ``approveTask`` threw the verify response away. So the surfacing reached every
verifier except the one it is for.

Same harness as ``test_board_view.py``: the page is a no-build vanilla-JS string and the
suite has no JS runtime, so these pin the page source's structure — the render sites, the
state, and the CSS.
"""

from __future__ import annotations

import re

from project_board.board_view import BOARD_PAGE


def _fn(name: str) -> str:
    """The source of one top-level ``function <name>(…){…}`` in the page."""
    start = BOARD_PAGE.index(f"function {name}(")
    return BOARD_PAGE[start : BOARD_PAGE.index("\n}\n", start)]


def test_the_drawer_renders_the_ledger_from_the_single_card_read_above_the_controls():
    """The ledger comes from the single-card GET the drawer already makes (the same read
    the deliverable comes from), and it renders ABOVE Approve/Reject — where the verifier
    is looking when they decide."""
    merge = "{...f, deliverable: d.feature.deliverable, requirements: d.feature.requirements}"
    assert merge in _fn("syncTaskDrawer")
    detail = _fn("taskDetail")
    assert "h += taskRequirements(f);" in detail
    assert detail.index("taskRequirements(f)") < detail.index("taskExtra(f)")  # above the buttons
    assert 'if (!reqs.length) return "";' in _fn("taskRequirements")  # no ledger, no section


def test_open_items_are_visually_distinct_and_every_field_is_escaped():
    """An open item wears the warning chip and the `treq--open` row class; done wears the
    success chip, declined a neutral one (with its reason). The header counts what is open.
    Ids, text, statuses and reasons are server-authored, so every one is esc()'d."""
    reqs = _fn("taskRequirements")
    assert 'const chip = closed ? (st === "done" ? " pl-badge--success" : "") : " pl-badge--warning";' in reqs
    assert "(closed ? '' : ' class=\"treq--open\"')" in reqs
    assert "requirements'+(open ? ' — '+open+' open' : '')" in reqs
    for field in ("esc(st)", 'esc(String(r.id || ""))', 'esc(String(r.text || ""))', "esc(String(r.decline_reason))"):
        assert field in reqs, field


def test_an_item_a_rejection_reopened_says_what_it_had_claimed():
    """A rejection reopens the items the refused round closed (#432 review), each keeping a
    `reopened_from` trace; the drawer shows it, so the verifier of the NEXT round can see
    the item was once claimed and refused — esc()'d like every other field."""
    reqs = _fn("taskRequirements")
    assert "r.reopened_from ?" in reqs
    assert "— reopened by a rejection (was '+esc(String(r.reopened_from))+')" in reqs


def test_approve_reads_the_verify_note_and_keeps_it_on_screen():
    """Approve used to discard the verify response. It now keeps the open-requirements
    `note`, and the drawer shows it in the page's existing notice idiom (a `pl-callout`,
    like the drawer's error callout) — through the reload and detail re-fetch that follow
    Approve and every 10s poll, until the drawer closes or switches task."""
    approve = _fn("approveTask")
    assert 'const r = await apiPost(FEAT+encodeURIComponent(fid)+"/verify", {approved: true});' in approve
    assert "VERIFY_NOTE = r && r.note ? {fid: fid, note: String(r.note)} : null;" in approve
    assert approve.index("VERIFY_NOTE =") < approve.index("await load(); await fetchTaskDetail(fid);")

    sync = _fn("syncTaskDrawer")
    assert "VERIFY_NOTE && VERIFY_NOTE.fid === TASK_FID" in sync  # this task's note only
    assert (
        "'<div class=\"pl-callout pl-callout--warning\"><b>Approved</b> with '+esc(VERIFY_NOTE.note)+'</div>'" in sync
    )

    assert "let VERIFY_NOTE = null;" in BOARD_PAGE
    assert "VERIFY_NOTE = null;" in _fn("openTask")  # a different task never inherits it
    assert "VERIFY_NOTE = null;" in _fn("closeMonitor")


def test_the_ledger_css_reuses_page_tokens_and_gives_the_new_one_a_fallback():
    """Every `--pl-*` token the ledger rules use is either one this stylesheet already
    relies on, or carries a fallback. An undefined `--pl-*` with no fallback renders as
    nothing, not as an error, so a missing token is invisible rather than broken."""
    css = BOARD_PAGE[BOARD_PAGE.index("<style>") : BOARD_PAGE.index("</style>")]
    rules = [ln for ln in css.splitlines() if ln.strip().startswith(".treq")]
    assert any(r.strip().startswith(".treq li.treq--open{") for r in rules)
    rest = "\n".join(ln for ln in css.splitlines() if not ln.strip().startswith(".treq"))
    known = set(re.findall(r"var\((--pl-[\w-]+)\)", rest))
    for rule in rules:
        for token, fallback in re.findall(r"var\((--pl-[\w-]+)(,[^)]*)?\)", rule):
            assert token in known or fallback, f"{token} in {rule.strip()!r} has no fallback"
    assert "font-weight:var(--pl-font-weight-medium, 500)" in css
