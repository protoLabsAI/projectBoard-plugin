"""Real-`gh` / real-GitHub integration tier for the 12 read-dominant ``worktree.py``
GitHub seams (#361, slice 2).

Every seam here shells the ACTUAL ``gh`` against a real OPEN PR in this repository — no
``_gh`` stub, no mocked API. Unlike ``test_worktree.py`` (which fakes ``_gh``/``_git`` to pin
the error-path branching) this tier validates what real GitHub actually returns for the
plugin's payloads: the class a mock is structurally blind to and that shipped #354 (a PAT
that cannot ``POST /check-runs``, green through every mock, publishing to nothing for a day).
A mock proves the call SHAPE; only the real API proves the configured credential holds the
permission and the provider behaves as assumed.

Posture mirrors the real-`br` tier (``test_integration.py``): local runs SKIP when GitHub
credentials are unavailable (``@requires_gh``); CI sets ``PB_REQUIRE_GH=1`` so an absent or
unusable credential — or an unresolvable fixture PR — FAILS via the guard test below rather
than silently skipping the whole tier. The CI job carries NO ``if:`` bypass: the READ seams run
on every event (fork PRs included) because a readable token + a resolvable OPEN PR is all they
need. The fixture PR + credential are resolved once by the ``gh_fixture`` fixture
(tests/conftest.py); the PR URL rides ``PB_GH_FIXTURE_PR``, which CI sets (see
.github/workflows/ci.yml) to the maintained ``PB_GH_FIXTURE_PR`` repo variable when a maintainer
pins a dedicated, permanently-open fixture PR, and otherwise to the PR under test (itself a real
open PR while its checks run). A PR URL is a public identifier, never a secret.

The two WRITES additionally need a write-capable token, so they gate on ``@requires_gh_write``
(``PB_GH_ALLOW_WRITES``): CI sets it on the write-capable paths (same-repo PR / push / dispatch)
and leaves it empty on fork PRs, whose GITHUB_TOKEN is forced read-only. On a fork PR the write
seams therefore SKIP with a documented reason — a structural credential limit, not the #354
silent skip — while every read seam still runs; the writes are covered by every same-repo PR and
every push, the paths the loop actually opens PRs from.

The 12 seams covered: ``repo_slug``, ``pr_state``, ``pr_head_sha``, ``pr_url_for_branch``,
``pr_merge_info``, ``pr_diff``, ``pr_ci_status``, ``post_review_status``,
``read_review_status``, ``_find_marked_comment``, ``post_or_update_pr_comment``, ``merge_pr``.
Reads use the pinned fixture. The only writes are (1) EXACTLY ONE commit status per run, keyed
on a STABLE disposable ``(context, sha)`` — GitHub records each POST as a new immutable entry
(the combined-status rollup, which the review gate reads, reports only the latest per context),
so the tier posts once and never re-posts to "prove idempotency", keeping the fixture head well
clear of GitHub's 1000-per-(sha, context) cap — and (2) ONE marked PR comment, updated in place
on a re-post via its hidden marker so a reconcile/retry never stacks a duplicate (r4).
``merge_pr`` is exercised with a DELIBERATELY wrong ``expected_head`` so GitHub refuses it
atomically: the seam runs for real (on every path, under a token that cannot merge), its
``--match-head-commit`` head-pin is proven, and the permanently-open fixture is never merged (r5).
"""

from __future__ import annotations

import os
import re

import pytest

from conftest import gh_tier_ready
from project_board import worktree

# The two deliberate writes target DISPOSABLE, STABLE identifiers — distinct from the
# production ``REVIEW_STATUS_CONTEXT`` / ``REVIEW_COMMENT_MARKER`` so the tier never disturbs a
# real review-gate signal on the fixture PR. Both are idempotent by construction: the commit
# status is keyed by ``(context, sha)`` (a re-post supersedes), the comment by its hidden marker
# (a re-post updates in place). One of each per PR head — no unbounded noise (r4).
TEST_STATUS_CONTEXT = "project-board/requires_gh-tier"
TEST_COMMENT_MARKER = "<!-- project-board:requires_gh-tier -->"

_HEX40 = re.compile(r"[0-9a-f]{40}")

_TIER_READY, _TIER_REASON = gh_tier_ready()
requires_gh = pytest.mark.skipif(
    not _TIER_READY,
    reason=_TIER_REASON or "real `gh` / real-GitHub tier prerequisites present (CI sets PB_REQUIRE_GH=1 to enforce)",
)

# The READ seams run on EVERY CI path (fork PRs included) — a readable token + a resolvable OPEN PR
# is all they need. The two idempotent WRITES additionally need a WRITE-capable token, which
# GITHUB_TOKEN is on same-repo PRs / pushes / dispatch but is FORCED read-only on fork PRs. So the
# write seams gate on PB_GH_ALLOW_WRITES, which CI sets only on the write-capable paths (see
# .github/workflows/ci.yml). On a fork PR the write seams SKIP with a documented reason — a
# structural credential limit, not the #354 silent skip — while the read seams still run under
# PB_REQUIRE_GH; they are covered by every same-repo PR + every push, the paths the loop opens PRs
# from. Locally set PB_GH_ALLOW_WRITES=1 (with a write-capable `gh` login) to exercise the writes.
_WRITES_ALLOWED = bool(os.environ.get("PB_GH_ALLOW_WRITES"))
if not _TIER_READY:
    _WRITE_REASON = _TIER_REASON
elif not _WRITES_ALLOWED:
    _WRITE_REASON = (
        "PB_GH_ALLOW_WRITES is not set — the two idempotent writes need a write-capable token "
        "(present on same-repo PRs / pushes / dispatch, but the fork-PR GITHUB_TOKEN is read-only); "
        "the read seams above still run and are enforced by PB_REQUIRE_GH"
    )
else:
    _WRITE_REASON = ""
requires_gh_write = pytest.mark.skipif(not (_TIER_READY and _WRITES_ALLOWED), reason=_WRITE_REASON or "writes enabled")


def _different_sha(sha: str) -> str:
    """A valid-format 40-hex sha guaranteed NOT to equal ``sha`` — a head the gate never
    examined, for the fail-safe head-identity checks (r5). Flips the first nibble."""
    return ("0" if sha[:1] != "0" else "1") + sha[1:]


def test_gh_tier_cannot_silently_skip_in_ci():
    """The skip guard (mirrors ``test_integration_tier_cannot_silently_skip_in_ci``, #136):
    deliberately NOT under ``@requires_gh``. When CI declares GitHub must be present
    (``PB_REQUIRE_GH=1``, set on the real-GitHub job), an absent/unusable credential or an
    unresolvable fixture PR FAILS here instead of silently skipping the whole tier — the way
    #354 (a PAT that cannot ``POST /check-runs``) shipped green through every mock. Without the
    env var (a local checkout with no gh auth) this is an always-green no-op, and the
    ``@requires_gh`` skips still apply to the real seam tests below."""
    if os.environ.get("PB_REQUIRE_GH"):
        ready, reason = gh_tier_ready()
        assert ready, (
            f"PB_REQUIRE_GH is set but the real-GitHub tier is not runnable: {reason}. The tier "
            f"would have silently skipped; fix the CI credential/fixture wiring (GH_TOKEN + "
            f"PB_GH_FIXTURE_PR — the maintained repo variable, or the PR under test) before "
            f"trusting this run."
        )


# ── repo_slug ────────────────────────────────────────────────────────────────────────


@requires_gh
async def test_repo_slug_resolves_the_checkout_repo(gh_fixture):
    """``repo_slug`` (``gh repo view --json nameWithOwner``) resolves the checkout's default
    GitHub repo to a well-formed ``owner/name`` — the read that #354 proved a mock cannot
    validate (it needs a real, authenticated `gh` against a real repo). Non-empty + exactly one
    ``/`` is the capability proof; the write tests below pin the exact fixture slug."""
    slug = await worktree.repo_slug(cwd=gh_fixture.repo_dir)
    assert slug and slug.count("/") == 1, f"repo_slug returned a non-slug: {slug!r}"


# ── pr_state ───────────────────────────────────────────────────────────────────────────


@requires_gh
async def test_pr_state_is_open_for_the_pinned_fixture(gh_fixture):
    """``pr_state`` reads ``OPEN`` for the permanently-open fixture — the exact signal the PR
    reconcile drives the board's Done/closed edges off. Matches the fixture's own authoritative
    read, so the seam and raw `gh` agree."""
    assert await worktree.pr_state(gh_fixture.url, cwd=gh_fixture.repo_dir) == "OPEN"


# ── pr_head_sha ─────────────────────────────────────────────────────────────────────────


@requires_gh
async def test_pr_head_sha_is_the_immutable_40_hex_head(gh_fixture):
    """``pr_head_sha`` (``headRefOid``) returns the PR's current 40-hex head — the immutable
    identity the review-gate reconcile (#328) pins a verdict to. It must equal the fixture's
    authoritative head (r5: head-bound reads agree on one head)."""
    head = await worktree.pr_head_sha(gh_fixture.url, cwd=gh_fixture.repo_dir)
    assert _HEX40.fullmatch(head), f"not a 40-hex sha: {head!r}"
    assert head == gh_fixture.head_sha


# ── pr_url_for_branch ───────────────────────────────────────────────────────────────────


@requires_gh
async def test_pr_url_for_branch_finds_the_fixture_pr(gh_fixture):
    """``pr_url_for_branch`` (crash recovery's "does this branch already have a PR?" read) maps
    the fixture's head branch back to the fixture PR url."""
    url = await worktree.pr_url_for_branch(gh_fixture.head_branch, cwd=gh_fixture.repo_dir)
    assert url.endswith(f"/pull/{gh_fixture.number}"), f"{url!r} is not the fixture PR"


# ── pr_merge_info ───────────────────────────────────────────────────────────────────────


@requires_gh
async def test_pr_merge_info_reports_a_well_typed_shape(gh_fixture):
    """``pr_merge_info`` reads ``{mergeStateStatus, isDraft}`` off ONE ``gh pr view`` — the
    merge-relevant facts the auto-merge/rebase edges consume. Against real GitHub it must parse
    to exactly those keys with the documented types (``mergeStateStatus`` a str, ``isDraft`` a
    ``bool`` or ``None``).

    It does NOT assert non-draft: the fixture contract (``gh_fixture``) requires the PR to be
    OPEN, not ready — a maintainer-pinned fixture or the PR-under-test may legitimately be an
    OPEN draft, so ``isDraft=True`` is a VALID value the seam must report faithfully, not one to
    reject. This test pins the shape/types the edges rely on; whether the fixture is a draft is
    the fixture's business, not this seam's."""
    info = await worktree.pr_merge_info(gh_fixture.url, cwd=gh_fixture.repo_dir)
    assert set(info) == {"mergeStateStatus", "isDraft"}
    assert isinstance(info["mergeStateStatus"], str)
    assert isinstance(info["isDraft"], bool) or info["isDraft"] is None


# ── pr_diff ─────────────────────────────────────────────────────────────────────────────


@requires_gh
async def test_pr_diff_returns_a_truncatable_unified_diff(gh_fixture):
    """``pr_diff`` returns the PR's unified diff (the prior attempt's work carried into an
    escalated re-dispatch) and truncates to ``max_chars`` with a marker. A real open PR has a
    non-empty diff; a tight cap really truncates."""
    full = await worktree.pr_diff(gh_fixture.url, cwd=gh_fixture.repo_dir)
    assert isinstance(full, str) and full, "expected a non-empty diff for the fixture PR"
    assert "diff --git" in full
    small = await worktree.pr_diff(gh_fixture.url, cwd=gh_fixture.repo_dir, max_chars=80)
    if len(full) > 80:
        assert small.endswith("…(diff truncated)")
        assert len(small) <= 80 + len("\n…(diff truncated)")


# ── pr_ci_status ────────────────────────────────────────────────────────────────────────


@requires_gh
async def test_pr_ci_status_reports_a_known_rollup(gh_fixture):
    """``pr_ci_status`` normalizes the real ``statusCheckRollup`` to one of
    ``passing/failing/pending/none`` plus a string summary — the closed-loop verify signal.
    The state varies with the fixture's live checks, so pin the CONTRACT (a known token, a
    string summary), not a particular colour."""
    status, summary = await worktree.pr_ci_status(gh_fixture.url, cwd=gh_fixture.repo_dir)
    assert status in {"passing", "failing", "pending", "none"}
    assert isinstance(summary, str)


# ── post_review_status + read_review_status (the commit-status write + its readback) ────


@requires_gh_write
async def test_post_and_read_review_status_round_trip_on_the_pinned_head(gh_fixture):
    """The #354 PAT-compatible verdict path, end-to-end against real GitHub: ``post_review_status``
    creates a COMMIT STATUS pinned to the fixture head (``POST /repos/{slug}/statuses/{sha}``) —
    the endpoint a user/PAT token CAN write, unlike #347's App-only check run — and
    ``read_review_status`` reads it back off the head-scoped combined-status endpoint. This is the
    exact capability #354 proved a mock cannot validate.

    EXACTLY ONE post per run (r4): GitHub records every status POST as a NEW immutable entry — a
    re-post is not an in-place update; only the combined-status ROLLUP reports the latest per
    context — and the permanently-open fixture's head is stable, so posting twice to "prove
    idempotency" would accrue two records every run toward GitHub's 1000-per-(sha, context) cap.
    The idempotency that matters here is that the write is CONSTRAINED to one STABLE disposable
    ``(context, sha)``: it never sprawls new contexts, and the rollup the review gate reads is
    latest-per-context — so a single post + readback IS the whole capability proof, and repeated
    CI runs converge on one live signal rather than accumulating distinct ones.

    Head-safe (r5/#328): the readback is scoped by the commit in its URL, so the verdict recorded
    for the fixture head is NEVER attributed to a DIFFERENT head — a wrong (but well-formed) sha
    reads back ``None`` even though the same context IS present on the true head. An EMPTY head
    posts nothing (no verdict against a head the gate never examined) and reads back ``None`` —
    without shelling gh."""
    slug, head, cwd = gh_fixture.slug, gh_fixture.head_sha, gh_fixture.repo_dir

    ok = await worktree.post_review_status(
        slug,
        head,
        state="success",
        description="requires_gh tier probe (stable disposable context)",
        target_url=gh_fixture.url,
        context=TEST_STATUS_CONTEXT,
        cwd=cwd,
    )
    assert ok is True

    read = await worktree.read_review_status(slug, head, context=TEST_STATUS_CONTEXT, cwd=cwd)
    assert read == {"state": "success", "head_sha": head, "passed": True}

    # Head-identity (r5), reusing the single write above: the verdict is present on the TRUE head,
    # yet a DIFFERENT well-formed head reads back None — the status is scoped by the commit in its
    # URL (no status at that sha, or the commit doesn't exist → gh errors) and is never attributed
    # to a moved head. No extra POST, so nothing else accumulates on the fixture.
    wrong = _different_sha(head)
    assert await worktree.read_review_status(slug, wrong, context=TEST_STATUS_CONTEXT, cwd=cwd) is None

    # Head-safe skips: an unknown head neither posts nor reads a verdict (no gh shelled).
    assert (
        await worktree.post_review_status(
            slug, "", state="success", description="x", context=TEST_STATUS_CONTEXT, cwd=cwd
        )
        is False
    )
    assert await worktree.read_review_status(slug, "", context=TEST_STATUS_CONTEXT, cwd=cwd) is None


# ── _find_marked_comment + post_or_update_pr_comment (the marked-comment write) ─────────


@requires_gh
async def test_find_marked_comment_absent_marker_returns_empty(gh_fixture):
    """``_find_marked_comment`` returns ``("", "")`` when no comment carries the marker — the
    signal that tells ``post_or_update_pr_comment`` to CREATE rather than PATCH. Proven against a
    real (``--paginate``-d) comment list with a marker that cannot exist on the fixture PR."""
    unique_marker = "<!-- project-board:requires_gh-absent-probe -->"
    found_id, found_body = await worktree._find_marked_comment(
        gh_fixture.slug, gh_fixture.number, unique_marker, cwd=gh_fixture.repo_dir
    )
    assert (found_id, found_body) == ("", "")


@requires_gh_write
async def test_post_or_update_pr_comment_is_idempotent_and_marked(gh_fixture):
    """``post_or_update_pr_comment`` posts — or idempotently UPDATES in place — a single
    board-authored PR comment identified by a hidden marker (#354). Against real GitHub the
    marked comment is found via ``_find_marked_comment`` and, on a re-post of the SAME body, is a
    no-op that never stacks a duplicate (r4: bounded to one comment per marker). An unparseable
    url posts nothing (False), so a bad url is never a side effect."""
    body = "requires_gh tier probe — idempotent marked comment (safe to leave; updated in place)."
    ok = await worktree.post_or_update_pr_comment(
        gh_fixture.url, body, marker=TEST_COMMENT_MARKER, cwd=gh_fixture.repo_dir
    )
    assert ok is True

    cid, cbody = await worktree._find_marked_comment(
        gh_fixture.slug, gh_fixture.number, TEST_COMMENT_MARKER, cwd=gh_fixture.repo_dir
    )
    assert cid, "the marked comment was not found after posting"
    assert TEST_COMMENT_MARKER in cbody and body in cbody

    # Re-post identical: idempotent no-op (byte-identical body → no PATCH), same comment id.
    ok_again = await worktree.post_or_update_pr_comment(
        gh_fixture.url, body, marker=TEST_COMMENT_MARKER, cwd=gh_fixture.repo_dir
    )
    assert ok_again is True
    cid_again, _ = await worktree._find_marked_comment(
        gh_fixture.slug, gh_fixture.number, TEST_COMMENT_MARKER, cwd=gh_fixture.repo_dir
    )
    assert cid_again == cid, "a re-post created a duplicate comment instead of updating in place"

    # An unparseable url posts nothing.
    assert (
        await worktree.post_or_update_pr_comment(
            "not-a-pr-url", body, marker=TEST_COMMENT_MARKER, cwd=gh_fixture.repo_dir
        )
        is False
    )


# ── merge_pr (exercised safely: a wrong expected_head forces an atomic refusal) ─────────


@requires_gh
async def test_merge_pr_refuses_a_head_that_moved_and_never_merges_the_fixture(gh_fixture):
    """``merge_pr`` shelled for real, WITHOUT merging the permanently-open fixture (r5). Passing a
    deliberately wrong ``expected_head`` makes ``gh pr merge --match-head-commit`` refuse the merge
    atomically (GitHub's ``expectedHeadOid`` mismatch) — the race-free half of the review gate's
    last-moment head pin (#323/#347): a merge must never land against a head the gate did not
    verify. It returns ``(False, detail)`` and the fixture stays OPEN. (The CI job also runs this
    with a ``contents: read`` token that cannot merge at all — belt-and-suspenders.)"""
    wrong = _different_sha(gh_fixture.head_sha)
    ok, detail = await worktree.merge_pr(gh_fixture.url, expected_head=wrong, cwd=gh_fixture.repo_dir)
    assert ok is False
    assert isinstance(detail, str) and detail
    # The fixture was NOT merged by the attempt above.
    assert await worktree.pr_state(gh_fixture.url, cwd=gh_fixture.repo_dir) == "OPEN"
