"""The attach edge's `gh` seam against a REAL open PR (#402): the real-GitHub tier.

`worktree.pr_identity` is classified REAL in tests/test_external_seams.py, so it must be
exercised against GitHub itself. A mock proves the call shape; only the API proves the
fields exist and mean what the attach decides on. The `test (real gh)` CI job runs this
file with PB_GH_FIXTURE_PR (the pinned permanently-open fixture PR, #370) and
PB_REQUIRE_GH=1, so an absent credential or fixture FAILS instead of skipping. That job
installs no `br`, so nothing here needs one: the refusal test hands the verb a card
projection directly, and the verb refuses before it would touch the store.
"""

from __future__ import annotations

import pytest

from project_board import worktree
from project_board.loop.attach import attach_external_pr
from project_board.store import BoardError


async def test_pr_identity_reads_which_pr_this_is(gh_fixture):
    """The facts the attach decides on, read from a REAL open PR: the canonical url (the
    form the merge webhook reports, so record_merge matches it), state, head, base, fork."""
    facts = await worktree.pr_identity(gh_fixture.url, cwd=gh_fixture.repo_dir)
    assert facts["url"].rstrip("/") == gh_fixture.url.rstrip("/")
    assert facts["state"] == "OPEN"
    assert facts["head"] == gh_fixture.head_branch
    assert isinstance(facts["base"], str) and facts["base"]
    assert isinstance(facts["cross_repo"], bool)


async def test_pr_identity_is_empty_for_a_pr_that_does_not_exist(gh_fixture):
    missing = gh_fixture.url.rsplit("/", 1)[0] + "/999999999"
    assert await worktree.pr_identity(missing, cwd=gh_fixture.repo_dir) == {}


async def test_a_real_pr_on_another_branch_is_refused_naming_the_branch_to_push_to(gh_fixture):
    """No fakes at the seam: the real fixture PR, whose head is not this card's branch. The
    refusal comes from the facts `gh` actually returned. The head check runs before the
    fork check, so it holds whether or not the fixture happens to come from a fork."""
    card = {
        "id": "bd-zz9",
        "title": "a card the fixture PR is not for",
        "issue_type": "feature",
        "board_state": "ready",
        "bead_status": "open",
        "labels": ["ready"],
        "pr_url": "",
        "open_depends_on": [],
    }

    with pytest.raises(BoardError) as exc:
        await attach_external_pr(None, card, gh_fixture.url, repo=gh_fixture.repo_dir, base="main")

    branch = worktree.branch_name(card["id"], card["title"])
    assert f"push the work to branch {branch!r}" in str(exc.value)
    assert gh_fixture.head_branch in str(exc.value)
