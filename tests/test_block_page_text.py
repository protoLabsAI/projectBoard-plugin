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
