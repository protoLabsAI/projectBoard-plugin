"""Block pages state the ask: blocked_reason rides along inside the 200-char budget (#gg9fa)."""

from project_board.block_pages import page_text

LINK = "/plugins/project_board/board?card=bd-7"
REASON = "needs a live dev server and real GM turns, run by the agent itself"


def feature(**overrides):
    return {"title": "Fix the build", "blocked_reason": REASON, **overrides}


def test_short_reason_appears_verbatim():
    assert page_text("bd-7", feature()) == (
        "bd-7: Fix the build is blocked: needs a live dev server and real GM turns, "
        f"run by the agent itself. [Open the card]({LINK})"
    )


def test_missing_reason_falls_back_to_todays_wording():
    assert page_text("bd-7", dict(title="Fix the build")) == (
        f"bd-7: Fix the build is blocked; [open the card]({LINK}) to review the reason and choose the next step."
    )


def test_long_reason_is_trimmed_with_an_ellipsis_and_the_link_survives():
    text = page_text("bd-7", feature(blocked_reason=REASON * 10))
    assert len(text) <= 200
    assert text.startswith("bd-7: Fix the build is blocked: needs a live dev server")
    assert "…" in text
    assert text.endswith(f" [Open the card]({LINK})")


def test_reason_whitespace_is_collapsed_and_no_double_period():
    text = page_text("bd-7", feature(blocked_reason="needs  a\n\tlive dev server."))
    assert "is blocked: needs a live dev server. [Open the card]" in text


def test_preflight_hold_carries_the_reason_the_same_way():
    assert page_text("bd-7", feature(blocked_class="preflight-hold")) == (
        "bd-7: Fix the build is waiting for its project check: needs a live dev server "
        f"and real GM turns, run by the agent itself. [Open the card]({LINK})"
    )


def test_preflight_hold_without_reason_keeps_todays_wording():
    assert page_text("bd-7", dict(title="Fix the build", blocked_class="preflight-hold")) == (
        f"bd-7: Fix the build is waiting for its project check; [open the card]({LINK}) to review it."
    )


def test_reason_is_trimmed_before_the_title_is_dropped():
    # The id appears twice (prefix + link): a long id shrinks the reason's room,
    # and the reason gives way first — the title stays while an ellipsis trims.
    fid = "bd-" + "x" * 27
    text = page_text(fid, feature(title="Totally descriptive title"))
    assert len(text) <= 200
    assert "Totally descriptive title is blocked: needs a live dev server" in text
    assert "…" in text
    assert text.endswith(f" [Open the card](/plugins/project_board/board?card={fid})")


def test_title_is_dropped_when_trimming_would_leave_almost_no_reason():
    fid = "bd-" + "x" * 27
    long_title = "Totally descriptive title that goes on and on about the build"
    text = page_text(fid, feature(title=long_title))
    assert len(text) <= 200
    assert "Totally descriptive title" not in text
    assert f"{fid} is blocked: {REASON}." in text
    assert text.endswith(f" [Open the card](/plugins/project_board/board?card={fid})")


# ── #801: lead with the cause; never cut away the last line of an error ──────────

# The page the operator actually got, which never said "disk":
#   bd-tndqq: S1: spend-cap route returns the daily soft (notice) lim is blocked: terminal:
#   worktree add failed: Preparing worktree (new branch… [Open the card](…)
TNDQQ_TITLE = "S1: spend-cap route returns the daily soft (notice) limit and the hard cap separately"
TNDQQ_REASON = (
    "terminal: worktree add failed: Preparing worktree (new branch 'feat/bd-tndqq')\n"
    "fatal: Unable to create '/Users/op/dev/app/.git/refs/heads/feat/bd-tndqq.lock': "
    "No space left on device"
)
TNDQQ_LINK = "/plugins/project_board/board?card=bd-tndqq"
DISK_LEAD = (
    "This computer is almost out of disk space, so this card can't start. "
    "Free up space and it will start again on its own."
)


def test_disk_full_block_leads_with_the_cause_and_the_fix_in_plain_words():
    # Still classed `terminal`, as the cards blocked before the host-disk class were.
    text = page_text("bd-tndqq", dict(title=TNDQQ_TITLE, blocked_reason=TNDQQ_REASON, blocked_class="terminal"))
    assert len(text) <= 200
    assert "disk space" in text
    assert text.startswith(DISK_LEAD)
    assert not text.startswith("bd-tndqq")
    # Title, then the link, then the card id last; no class and no raw git error.
    assert text.endswith(f" [Open the card]({TNDQQ_LINK}) bd-tndqq")
    assert f"{DISK_LEAD} S1: spend" in text
    assert "terminal" not in text and "worktree add failed" not in text


def test_host_disk_class_gets_the_same_page_whatever_its_reason_says():
    for reason in (TNDQQ_REASON, "setup_cmd exited 2: failed to rename file", ""):
        text = page_text("bd-7", dict(title="Fix the build", blocked_reason=reason, blocked_class="host-disk"))
        assert text == f"{DISK_LEAD} Fix the build [Open the card]({LINK}) bd-7"
        assert len(text) <= 200


def test_host_disk_page_keeps_the_lead_and_link_on_a_very_long_card_id():
    # No room for the title or a second copy of the id: the link already carries it.
    fid = "bd-" + "x" * 27
    text = page_text(fid, dict(title=TNDQQ_TITLE, blocked_reason=TNDQQ_REASON, blocked_class="host-disk"))
    assert len(text) <= 200
    assert text == f"{DISK_LEAD} [Open the card](/plugins/project_board/board?card={fid})"


def test_long_reason_keeps_its_last_clause_whole_and_trims_the_middle():
    reason = (
        "worktree add failed: Preparing worktree (new branch 'feat/bd-7')\n"
        "fatal: could not create leading directories of '/Users/op/dev/app/.worktrees/feat-bd-7/.git': "
        "Read-only file system"
    )
    text = page_text("bd-7", feature(blocked_reason=reason))
    assert len(text) <= 200
    assert text.startswith("bd-7: Fix the build is blocked: worktree add failed: Preparing worktree")
    assert text.endswith(f"… Read-only file system. [Open the card]({LINK})")


def test_last_line_of_a_multi_line_error_survives_without_a_colon():
    reason = "gate failed:\n" + "tests/test_a.py::test_x PASSED\n" * 12 + "FAILED tests/test_b.py::test_y"
    text = page_text("bd-7", feature(blocked_reason=reason))
    assert len(text) <= 200
    assert text.endswith(f"… FAILED tests/test_b.py::test_y. [Open the card]({LINK})")


def test_title_gives_way_before_the_last_clause_is_cut():
    fid = "bd-" + "x" * 27
    cause = "the release branch is protected and needs an admin to push"
    text = page_text(fid, feature(title="Totally descriptive title", blocked_reason="push failed: " * 9 + cause))
    assert len(text) <= 200
    assert "Totally descriptive title" not in text
    assert text.endswith(f"… {cause}. [Open the card](/plugins/project_board/board?card={fid})")


def test_preflight_hold_and_review_coverage_pages_are_not_reworded_by_a_quoted_disk_error():
    held = page_text("bd-7", feature(blocked_class="preflight-hold", blocked_reason="gate: No space left on device"))
    assert held == (
        f"bd-7: Fix the build is waiting for its project check: gate: No space left on device. [Open the card]({LINK})"
    )
    reason = "Incomplete review coverage after re-review: https://example.test/pr/1 at abc123 (ENOSPC)"
    review = page_text("bd-7", feature(blocked_class="needs-review", blocked_reason=reason))
    assert review == f"bd-7 is blocked: {reason}. [Open the card]({LINK})"
