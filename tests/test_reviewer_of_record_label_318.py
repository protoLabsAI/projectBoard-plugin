"""pr-reviewer#318: with the review gate on, the board labels its PR as the board's to review,
so the fleet QA panel carries the gate's verdict instead of spending a second panel."""

from __future__ import annotations

import pytest

from project_board import worktree
from project_board.loop import BoardLoop

from test_draft_until_review_691 import _first_open_kwargs  # noqa: F401
from test_loop import _no_real_pr_head_sha, _no_real_review_status_seam  # noqa: F401

ROR = "reviewer-of-record:board"


def test_the_label_knob_defaults_on_and_can_be_cleared():
    assert BoardLoop({}).reviewer_of_record_label == ROR
    assert BoardLoop({"reviewer_of_record_label": ""}).reviewer_of_record_label == ""
    assert BoardLoop({"reviewer_of_record_label": " custom "}).reviewer_of_record_label == "custom"


@pytest.mark.parametrize(
    "cfg,labelled",
    [
        ({"review_gate": True}, True),
        ({"review_gate": True, "reviewer_of_record_label": ""}, False),
        ({}, False),  # no gate, no verdict to hand over: never label
    ],
)
async def test_only_a_gated_board_labels_its_pr(monkeypatch, cfg, labelled):
    seen = await _first_open_kwargs(monkeypatch, cfg)
    assert seen
    assert ("labels" in seen[0]) is labelled
    if labelled:
        assert seen[0]["labels"] == (ROR,)


class _Shell:
    def __init__(self, *, refuse_label=False):
        self.calls: list[tuple] = []
        self.refuse_label = refuse_label

    async def gh(self, *args, cwd="."):
        self.calls.append(args)
        if args[:2] == ("pr", "create"):
            if "--label" in args and self.refuse_label:
                return 1, "", "could not add label: 'reviewer-of-record:board' not found"
            return 0, "https://github.com/o/r/pull/9\n", ""
        return 0, "", ""

    async def git(self, wt, *args, timeout=60):
        if args[:2] == ("rev-list", "--count"):
            return 0, "1\n", ""
        return 0, "", ""


@pytest.fixture
def shell(monkeypatch):
    sh = _Shell()

    async def _commit(wt, title):
        return None

    monkeypatch.setattr(worktree, "commit_worktree", _commit)
    monkeypatch.setattr(worktree, "_gh", sh.gh)
    monkeypatch.setattr(worktree, "_git", sh.git)
    return sh


async def test_open_pr_creates_the_label_then_opens_the_pr_with_it(shell):
    url = await worktree.open_pr("/wt", "feat/x", title="t", body="b", labels=(ROR,))
    assert url == "https://github.com/o/r/pull/9"
    creates = [c for c in shell.calls if c[:2] == ("label", "create")]
    assert creates and creates[0][2] == ROR and "--force" in creates[0]
    pr = [c for c in shell.calls if c[:2] == ("pr", "create")]
    assert len(pr) == 1 and pr[0][pr[0].index("--label") + 1] == ROR


async def test_a_refused_label_never_costs_the_pr(shell):
    shell.refuse_label = True
    url = await worktree.open_pr("/wt", "feat/x", title="t", body="b", labels=(ROR,))
    assert url == "https://github.com/o/r/pull/9"
    pr = [c for c in shell.calls if c[:2] == ("pr", "create")]
    assert len(pr) == 2 and "--label" in pr[0] and "--label" not in pr[1]


async def test_no_labels_makes_no_label_calls(shell):
    await worktree.open_pr("/wt", "feat/x", title="t", body="b")
    assert not [c for c in shell.calls if c[0] == "label"]
    assert "--label" not in [c for c in shell.calls if c[:2] == ("pr", "create")][0]
