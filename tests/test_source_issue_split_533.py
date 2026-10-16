"""#533: a source issue split across several cards must be closed only by the LAST
open slice, never by the first merged one.

Two closing paths are covered:
  1. the PR body — ``Fixes #N`` becomes ``Refs #N`` while any open sibling shares the
     issue (``_source_issue_has_open_sibling`` + ``_inject_source_issue_line(refs_only=)``,
     wired through the drive's ``_with_source_issue_ref``);
  2. the squash commit subject (``feat: {title}``) — a title like ``fix: #6.3`` would
     make GitHub read ``#6`` as a closing reference, so ``_sanitize_pr_title`` drops the
     ``#`` on any foreign closing token.
"""

from __future__ import annotations

from project_board import worktree
from project_board.loop import (
    BoardLoop,
    _inject_source_issue_line,
    _sanitize_pr_title,
    _slice_work_uncaptured,
    _source_issue_has_open_sibling,
)


class _FakeStore:
    """Minimal board store: ``list_features()`` returns pre-seeded rows."""

    def __init__(self, features):
        self.features = list(features)

    def list_features(self, state=None, include_archived=False):
        rows = self.features
        if state is not None:
            rows = [f for f in rows if f.get("board_state") == state]
        return list(rows)


class _BoomStore:
    """A store whose read raises — exercises the fail-SAFE path."""

    def list_features(self, state=None, include_archived=False):
        raise RuntimeError("board unreachable")


# ── _inject_source_issue_line: the refs_only switch ──────────────────────────────


def test_inject_refs_only_stamps_bare_refs_same_repo():
    body = "## Summary\n\n- did the thing"
    out = _inject_source_issue_line(body, "acme/widgets", 6, "acme/widgets", refs_only=True)
    assert out == body + "\n\nRefs #6"
    # Idempotent: re-stamping its own output does not duplicate the line.
    assert _inject_source_issue_line(out, "acme/widgets", 6, "acme/widgets", refs_only=True) == out


def test_inject_default_still_fixes_same_repo():
    # refs_only defaults off → the historical Fixes #n behaviour is unchanged.
    out = _inject_source_issue_line("body", "acme/widgets", 6, "acme/widgets")
    assert out == "body\n\nFixes #6"


def test_inject_refs_only_is_noop_cross_repo():
    # Cross-repo is always a Refs <url> link; the flag changes nothing there (r5).
    out = _inject_source_issue_line("body", "other/repo", 9, "acme/widgets", refs_only=True)
    assert out == "body\n\nRefs https://github.com/other/repo/issues/9"


def test_inject_refs_only_rewrites_existing_fixes_to_refs():
    # A coder-written `Fixes #6` in the body would STILL close the issue on merge — in
    # refs_only mode it is rewritten to `Refs #6`, not left in place, and no extra line
    # is appended. This is what makes refs_only actually prevent the close.
    body = "## Summary\n\nDid the thing. Fixes #6 for real."
    out = _inject_source_issue_line(body, "acme/widgets", 6, "acme/widgets", refs_only=True)
    assert "Fixes #6" not in out
    assert out == "## Summary\n\nDid the thing. Refs #6 for real."


def test_inject_refs_only_rewrites_closes_keyword():
    out = _inject_source_issue_line("Closes #6", "acme/widgets", 6, "acme/widgets", refs_only=True)
    assert out == "Refs #6"


# ── _source_issue_has_open_sibling ───────────────────────────────────────────────


def test_has_open_sibling_true_when_another_open_card_shares_issue():
    """r1: a second non-terminal card carrying the same issue ⇒ stamp Refs."""
    feature = {"id": "bd-1", "source_issue": "#6"}
    store = _FakeStore(
        [
            {"id": "bd-1", "board_state": "in_progress", "source_issue": "#6"},
            {"id": "bd-2", "board_state": "ready", "source_issue": "#6"},
        ]
    )
    assert _source_issue_has_open_sibling(store, feature) is True


def test_has_open_sibling_false_when_siblings_terminal():
    """r2: every other card for #6 is done/cancelled ⇒ this is the last open slice
    ⇒ stamp Fixes."""
    feature = {"id": "bd-1", "source_issue": "#6"}
    store = _FakeStore(
        [
            {"id": "bd-1", "board_state": "in_progress", "source_issue": "#6"},
            {"id": "bd-2", "board_state": "done", "source_issue": "#6", "pr_url": "https://x/1"},
            {"id": "bd-3", "board_state": "cancelled", "source_issue": "#6"},
        ]
    )
    assert _source_issue_has_open_sibling(store, feature) is False


def test_has_open_sibling_excludes_self():
    feature = {"id": "bd-1", "source_issue": "#6"}
    store = _FakeStore([{"id": "bd-1", "board_state": "in_progress", "source_issue": "#6"}])
    assert _source_issue_has_open_sibling(store, feature) is False


def test_has_open_sibling_normalises_reference_forms():
    # A URL sibling matches a bare-#n current: the compare is on parsed (slug, n).
    feature = {"id": "bd-1", "source_issue": "#6"}
    store = _FakeStore(
        [
            {"id": "bd-1", "board_state": "in_progress", "source_issue": "#6"},
            {"id": "bd-2", "board_state": "ready", "source_issue": "https://github.com/acme/widgets/issues/6"},
        ]
    )
    # A bare #6 parses to ("", 6); the URL parses to ("acme/widgets", 6) — different slugs,
    # so they do NOT match. A same-form sibling does.
    assert _source_issue_has_open_sibling(store, feature) is False
    store.features.append({"id": "bd-3", "board_state": "ready", "source_issue": "6"})
    assert _source_issue_has_open_sibling(store, feature) is True


def test_has_open_sibling_false_when_no_source_issue():
    assert _source_issue_has_open_sibling(_FakeStore([]), {"id": "bd-1", "title": "no issue"}) is False


def test_has_open_sibling_false_when_sibling_in_review():
    """Review fix: a sibling already ``in_review`` has its work captured in an OPEN PR and
    must NOT block the close. Counting it as open made two slices that reach review at the
    same time BOTH stamp ``Refs`` — then neither closed the issue and it stayed open after
    all slices merged. The slice building its PR here is the last one still coding, so it
    carries ``Fixes``."""
    feature = {"id": "bd-1", "source_issue": "#6"}
    store = _FakeStore(
        [
            {"id": "bd-1", "board_state": "in_progress", "source_issue": "#6"},
            {"id": "bd-2", "board_state": "in_review", "source_issue": "#6", "pr_url": "https://x/2"},
        ]
    )
    assert _source_issue_has_open_sibling(store, feature) is False


def test_has_open_sibling_false_when_sibling_has_pr_url():
    """Belt-and-braces: any sibling carrying a ``pr_url`` has its work in a PR regardless
    of its lane, so it never blocks the close."""
    feature = {"id": "bd-1", "source_issue": "#6"}
    store = _FakeStore(
        [
            {"id": "bd-1", "board_state": "in_progress", "source_issue": "#6"},
            {"id": "bd-2", "board_state": "blocked", "source_issue": "#6", "pr_url": "https://x/2"},
        ]
    )
    assert _source_issue_has_open_sibling(store, feature) is False


def test_has_open_sibling_true_when_coding_sibling_alongside_in_review():
    """A mix: one sibling is in_review (captured, non-blocking) but another is still being
    coded (no PR) ⇒ that still-coding slice keeps the issue open ⇒ ``Refs``."""
    feature = {"id": "bd-1", "source_issue": "#6"}
    store = _FakeStore(
        [
            {"id": "bd-1", "board_state": "in_progress", "source_issue": "#6"},
            {"id": "bd-2", "board_state": "in_review", "source_issue": "#6", "pr_url": "https://x/2"},
            {"id": "bd-3", "board_state": "ready", "source_issue": "#6"},
        ]
    )
    assert _source_issue_has_open_sibling(store, feature) is True


def test_slice_work_uncaptured_classification():
    """``_slice_work_uncaptured`` is the one predicate the open-sibling scan turns on:
    only a still-coding slice (non-terminal, not in_review, no pr_url) is uncaptured."""
    assert _slice_work_uncaptured({"board_state": "in_progress"}) is True
    assert _slice_work_uncaptured({"board_state": "ready"}) is True
    assert _slice_work_uncaptured({"board_state": "backlog"}) is True
    assert _slice_work_uncaptured({"board_state": "blocked"}) is True
    # Captured: PR open, merged, abandoned, or a pr_url present on any lane.
    assert _slice_work_uncaptured({"board_state": "in_review", "pr_url": "https://x/2"}) is False
    assert _slice_work_uncaptured({"board_state": "in_review"}) is False
    assert _slice_work_uncaptured({"board_state": "done", "pr_url": "https://x/1"}) is False
    assert _slice_work_uncaptured({"board_state": "cancelled"}) is False
    assert _slice_work_uncaptured({"board_state": "in_progress", "pr_url": "https://x/3"}) is False


def test_has_open_sibling_fails_safe_on_store_error():
    """r3: a store read error returns True ⇒ the caller stamps Refs, never Fixes —
    the opposite fail direction from the fail-OPEN guards, so uncertainty never closes
    an issue early."""
    assert _source_issue_has_open_sibling(_BoomStore(), {"id": "bd-1", "source_issue": "#6"}) is True


# ── _sanitize_pr_title: keep foreign closing refs out of the squash subject ───────


def test_sanitize_strips_foreign_slice_tag():
    """r4: `fix: #6.3 address review` loses the `#` so the squash subject can't close
    #6; the slice digits are preserved."""
    out = _sanitize_pr_title("fix: #6.3 address review", None)
    assert out == "fix: 6.3 address review"
    assert "#6" not in out


def test_sanitize_strips_slice_tag_even_when_digits_match_own():
    # A slice tag (#6.3) is never a clean self-reference — GitHub reads the leading #6 —
    # so it is neutralised even when its digits equal the card's own issue.
    out = _sanitize_pr_title("fix: #6.3 address review", ("", 6))
    assert out == "fix: 6.3 address review"


def test_sanitize_keeps_clean_own_reference():
    """r4: a card whose own clean source issue appears in its title is unchanged —
    the last open slice is meant to close it."""
    title = "fix #6 the login bug"
    assert _sanitize_pr_title(title, ("", 6)) == title


def test_sanitize_strips_foreign_clean_reference():
    # A clean #42 that is NOT this card's issue still closes #42 from the subject → drop it.
    assert _sanitize_pr_title("fixes #42 unrelated", ("", 6)) == "fixes 42 unrelated"


def test_sanitize_cross_repo_own_is_not_protected():
    # A bare #6 in a title can never name a cross-repo issue, so when the source lives
    # elsewhere every closing #n in the title is foreign and gets neutralised.
    assert _sanitize_pr_title("fix #6 thing", ("other/repo", 6)) == "fix 6 thing"


def test_sanitize_untouched_without_closing_lead():
    # No closing keyword / conventional-commit prefix before the #n → left alone (a plain
    # `feat:` squash prefix does not close), and `fix` inside another word is not a lead.
    assert _sanitize_pr_title("add support for #6 handling", ("", 99)) == "add support for #6 handling"
    assert _sanitize_pr_title("prefix #6 cleanup", ("", 99)) == "prefix #6 cleanup"


# ── _with_source_issue_ref: the two helpers wired together on a drive ─────────────


def _loop_with_store(monkeypatch, store, *, slug="acme/widgets"):
    loop = BoardLoop({})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    async def _slug(*, cwd):
        return slug

    monkeypatch.setattr(worktree, "repo_slug", _slug)
    return loop


async def test_with_source_issue_ref_refs_when_open_sibling(monkeypatch):
    """r1 end-to-end: two open cards share #6 ⇒ the first PR body gets Refs #6."""
    feature = {"id": "bd-1", "source_issue": "#6"}
    store = _FakeStore(
        [
            {"id": "bd-1", "board_state": "in_progress", "source_issue": "#6"},
            {"id": "bd-2", "board_state": "ready", "source_issue": "#6"},
        ]
    )
    loop = _loop_with_store(monkeypatch, store)
    out = await loop._with_source_issue_ref(feature, "/wt", "## Summary")
    assert out == "## Summary\n\nRefs #6"


async def test_with_source_issue_ref_fixes_when_sole_open(monkeypatch):
    """r2 end-to-end: the remaining card is the only open one for #6 ⇒ Fixes #6."""
    feature = {"id": "bd-1", "source_issue": "#6"}
    store = _FakeStore(
        [
            {"id": "bd-1", "board_state": "in_progress", "source_issue": "#6"},
            {"id": "bd-2", "board_state": "done", "source_issue": "#6", "pr_url": "https://x/1"},
        ]
    )
    loop = _loop_with_store(monkeypatch, store)
    out = await loop._with_source_issue_ref(feature, "/wt", "## Summary")
    assert out == "## Summary\n\nFixes #6"


async def test_with_source_issue_ref_fixes_when_sibling_in_review(monkeypatch):
    """Review fix, end-to-end: the one remaining sibling is already in_review (its PR is
    open), so the slice building its PR here is the last one still coding and gets
    ``Fixes #6`` — not ``Refs``. The old behaviour left BOTH slices on ``Refs`` and the
    issue never closed."""
    feature = {"id": "bd-1", "source_issue": "#6"}
    store = _FakeStore(
        [
            {"id": "bd-1", "board_state": "in_progress", "source_issue": "#6"},
            {"id": "bd-2", "board_state": "in_review", "source_issue": "#6", "pr_url": "https://x/2"},
        ]
    )
    loop = _loop_with_store(monkeypatch, store)
    out = await loop._with_source_issue_ref(feature, "/wt", "## Summary")
    assert out == "## Summary\n\nFixes #6"


async def test_with_source_issue_ref_refs_on_store_error(monkeypatch):
    """r3 end-to-end: a store read error while checking siblings ⇒ Refs #6."""
    feature = {"id": "bd-1", "source_issue": "#6"}
    loop = _loop_with_store(monkeypatch, _BoomStore())
    out = await loop._with_source_issue_ref(feature, "/wt", "## Summary")
    assert out == "## Summary\n\nRefs #6"


async def test_with_source_issue_ref_cross_repo_refs_url(monkeypatch):
    """r5 end-to-end: a cross-repo source issue still gets Refs <full-url>, even with
    an open sibling."""
    feature = {"id": "bd-1", "source_issue": "other/repo#9"}
    store = _FakeStore(
        [
            {"id": "bd-1", "board_state": "in_progress", "source_issue": "other/repo#9"},
            {"id": "bd-2", "board_state": "ready", "source_issue": "other/repo#9"},
        ]
    )
    loop = _loop_with_store(monkeypatch, store)
    out = await loop._with_source_issue_ref(feature, "/wt", "## Summary")
    assert out == "## Summary\n\nRefs https://github.com/other/repo/issues/9"
