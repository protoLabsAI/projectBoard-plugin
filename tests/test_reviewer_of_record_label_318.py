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


class _ExistingShell(_Shell):
    """`gh pr create` reports the PR already exists (a coder-opened draft)."""

    async def gh(self, *args, cwd="."):
        self.calls.append(args)
        if args[:2] == ("pr", "create"):
            return 1, "", 'a pull request for branch "feat/x" into branch "main" already exists'
        if args[:2] == ("pr", "view"):
            return 0, "https://github.com/o/r/pull/9\n", ""
        return 0, "", ""


@pytest.fixture
def existing(monkeypatch):
    sh = _ExistingShell()

    async def _commit(wt, title):
        return None

    async def _promote(url, branch, *, cwd, adopted=True):
        return True

    monkeypatch.setattr(worktree, "commit_worktree", _commit)
    monkeypatch.setattr(worktree, "_promote_adopted_draft", _promote)
    monkeypatch.setattr(worktree, "_gh", sh.gh)
    monkeypatch.setattr(worktree, "_git", sh.git)
    return sh


async def test_an_adopted_existing_pr_gets_the_label_on_first_adoption(existing):
    url = await worktree.open_pr("/wt", "feat/x", title="t", body="b", labels=(ROR,))
    assert url == "https://github.com/o/r/pull/9"
    edits = [c for c in existing.calls if c[:2] == ("pr", "edit")]
    assert edits == [("pr", "edit", url, "--add-label", ROR)]


async def test_a_draft_salvage_of_an_existing_pr_is_labelled_too(existing):
    url = await worktree.open_pr("/wt", "feat/x", title="t", body="b", promote_draft=False, draft=True, labels=(ROR,))
    assert [c for c in existing.calls if c[:2] == ("pr", "edit")] == [("pr", "edit", url, "--add-label", ROR)]


async def test_a_redispatch_never_relabels_the_cards_own_pr(existing):
    """An operator who removed the label to ask for the panel's own review keeps it removed."""
    await worktree.open_pr("/wt", "feat/x", title="t", body="b", promote_draft=False, labels=(ROR,))
    assert not [c for c in existing.calls if c[:2] == ("pr", "edit")]


async def test_reconcile_publishes_a_stranded_tree_with_the_label(monkeypatch):
    seen = []

    async def fake_open_pr(wt, branch, **kwargs):
        seen.append(kwargs)
        return "https://github.com/o/r/pull/9"

    monkeypatch.setattr(worktree, "open_pr", fake_open_pr)
    for cfg, labelled in (({"review_gate": True}, True), ({}, False)):
        seen.clear()
        loop = BoardLoop(cfg)

        async def no_ref(f, wt, body):
            return body, False

        monkeypatch.setattr(loop, "_with_source_issue_ref", no_ref)
        monkeypatch.setattr(loop, "_base_branch_for", lambda f: "main")
        await loop._open_tree_pr({"id": "bd-x", "title": "t"}, "/wt", "feat/bd-x")
        assert ("labels" in seen[0]) is labelled
        if labelled:
            assert seen[0]["labels"] == (ROR,)
        # A card that already owns its PR: neither a republish nor a red-gate salvage labels.
        for gate_out in (None, "red gate"):
            seen.clear()
            await loop._open_tree_pr(
                {"id": "bd-x", "title": "t", "pr_url": "https://github.com/o/r/pull/9"},
                "/wt",
                "feat/bd-x",
                gate_out=gate_out,
            )
            assert "labels" not in seen[0]
