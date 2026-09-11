"""Publish a stranded card's worktree without a coder (#427).

bd-ezs7's coder finished 170 lines and died before publishing them. Recovering that work
needed a CODER — every publication step was only reachable by dispatching one — and that
day the coder delegate was down for an unrelated reason, so finished work sat behind an
outage that had nothing to do with it. The operator salvage runs the board's own tail
instead: commit, the pre-PR gate, push, PR, review.

Real git throughout — a bare origin plus a clone; the tree's commit and the push to that
origin are real — and a REAL `br` board, so every card transition is the store's own.
Only `gh` is faked, at the `_gh` seam: opening a real PR needs a disposable GitHub repo
(see test_external_seams.py's EXEMPT seams). The fake models what the salvage must cope
with — a PR that already exists, a repo without drafts, a conversion GitHub refuses.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from project_board import store as store_mod
from project_board import worktree
from project_board.loop import BoardLoop, live_drive
from project_board.store import LABEL_READY, BeadsBoard

_TITLE = "Make the poll timeout progress-based"
_URL = "https://github.com/o/r/pull/9"
_RED = "echo 'FAILED tests/test_poll.py::test_it - boom'; exit 1"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _identity(repo: str) -> None:
    _git("-C", repo, "config", "user.email", "board-test@localhost")
    _git("-C", repo, "config", "user.name", "Board Test")
    _git("-C", repo, "config", "commit.gpgsign", "false")


class _Origin:
    base = "main"

    def __init__(self, tmp_path: Path):
        self.origin = str(tmp_path / "origin.git")
        seed = str(tmp_path / "seed")
        self.clone = str(tmp_path / "clone")
        _git("init", "--bare", self.origin)
        _git("init", "-b", self.base, seed)
        _identity(seed)
        Path(seed, "README.md").write_text("base\n")
        Path(seed, "a.py").write_text("a = 0\n")  # the card's files_to_modify: the Ready gate checks it exists
        Path(seed, ".gitignore").write_text(".beads/\n")
        _git("-C", seed, "add", "-A")
        _git("-C", seed, "commit", "-m", "base commit")
        _git("-C", seed, "remote", "add", "origin", self.origin)
        _git("-C", seed, "push", "-u", "origin", self.base)
        _git("-C", self.origin, "symbolic-ref", "HEAD", f"refs/heads/{self.base}")
        _git("clone", self.origin, self.clone)
        _identity(self.clone)

    def pushed(self) -> list[str]:
        """The feature branches that reached the origin."""
        out = _git("-C", self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/feat/")
        return [line for line in out.splitlines() if line]

    def show(self, ref: str, rel: str) -> str:
        return _git("-C", self.origin, "show", f"{ref}:{rel}") + "\n"


class _Gh:
    """`gh`, faked at the seam. ``existing``: a PR is already open for the branch, so
    `pr create` fails "already exists". ``drafts``: the repo supports draft PRs."""

    url = _URL

    def __init__(self, *, existing: bool = False, drafts: bool = True):
        self.existing, self.drafts = existing, drafts
        self.is_draft = False
        self.closed = False
        self.calls: list[tuple] = []
        self.comments: list[str] = []

    def made(self, *head: str) -> list[tuple]:
        return [c for c in self.calls if c[: len(head)] == head]

    async def __call__(self, *args, cwd, timeout=60):
        self.calls.append(args)
        a = list(args)
        if a[:2] == ["pr", "create"]:
            if self.existing:
                head = a[a.index("--head") + 1]
                return 1, "", f'a pull request for branch "{head}" into branch "main" already exists:\n{self.url}\n'
            if "--draft" in a and not self.drafts:
                return 1, "", "GraphQL: Draft pull requests are not supported in this repository. (createPullRequest)"
            self.existing, self.is_draft = True, "--draft" in a
            return 0, self.url + "\n", ""
        if a[:2] == ["pr", "view"]:
            if "url" in a:
                return (0, self.url + "\n", "") if self.existing else (1, "", "no pull requests found")
            if "isDraft,mergeStateStatus" in a:
                return 0, json.dumps({"isDraft": self.is_draft, "mergeStateStatus": "CLEAN"}), ""
            if "state" in a:
                return 0, ("CLOSED" if self.closed else "OPEN") + "\n", ""
        if a[:2] == ["pr", "ready"] and "--undo" in a:
            if not self.drafts:
                return 1, "", "GraphQL: Draft pull requests are not supported in this repository."
            self.is_draft = True
            return 0, "", ""
        if a[:2] == ["pr", "close"]:
            self.closed = True
            return 0, "", ""
        if a[0] == "api":
            if "--method" in a:
                self.comments.append(next(x for x in a if x.startswith("body="))[len("body=") :])
                return 0, "{}", ""
            return 0, "[]", ""  # the PR's comments: none yet
        return 1, "", f"unexpected gh call {args}"


@pytest.fixture
def origin(tmp_path):
    return _Origin(tmp_path)


def _setup(origin: _Origin, monkeypatch, *, gate: str = "true", review_gate: bool = False, gh: _Gh | None = None):
    """A REAL board over the clone, the card on it made `ready`, and a loop over both."""
    if shutil.which(store_mod.BR) is None:  # CI installs the pinned `br` (see test_integration.py)
        pytest.skip("real `br` (beads) CLI not on PATH")
    board = BeadsBoard(repo=origin.clone, actor="test")
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: board)
    gh = gh or _Gh()
    monkeypatch.setattr(worktree, "_gh", gh)
    loop = BoardLoop({"coder": "proto", "local_gate_cmd": gate, "review_gate": review_gate, "merge_poll": False})
    reviews: list[str] = []

    async def _review(store, fid, pr_url, repo):
        reviews.append(pr_url)

    monkeypatch.setattr(loop, "_review_gate", _review)
    f = board.create_feature(
        _TITLE, spec="s", acceptance_criteria="- WHEN x THE SYSTEM SHALL y", files_to_modify=["a.py"]
    )
    board.mark_ready(f["id"])
    return board, gh, loop, f["id"], reviews


def _canon(fid: str) -> str:
    return worktree.branch_name(fid, _TITLE)


async def _stranded_candidate(origin: _Origin, fid: str, suffix: str = "g1", marker: str = "g1") -> str:
    """A candidate whose coder finished and died: edits never committed, never pushed."""
    wt, _ = await worktree.create_worktree(origin.clone, origin.base, f"{fid}.{suffix}")
    Path(wt, "README.md").write_text(f"finished work ({marker})\n")
    Path(wt, "adapters.py").write_text(f"POLL = '{marker}'\n")
    return wt


async def _canonical_with_pr(origin: _Origin, fid: str) -> str:
    """A canonical tree the first drive already pushed and opened its PR from."""
    wt, branch = await worktree.create_worktree(origin.clone, origin.base, fid, title=_TITLE)
    Path(wt, "a.py").write_text("first attempt\n")
    _git("-C", wt, "add", "-A")
    _git("-C", wt, "commit", "-qm", "first attempt")
    _git("-C", wt, "push", "-q", "-u", "origin", branch)
    return wt


async def _ci_fix_round_died(origin: _Origin, board: BeadsBoard, fid: str) -> str:
    """A card parked in_progress with its PR — bounced for red CI, and the CI-fix coder died
    leaving its fix uncommitted in the canonical tree."""
    board.claim(fid, assignee="proto")
    wt = await _canonical_with_pr(origin, fid)
    board.open_review(fid, pr_url=_URL)
    board.bounce_ci_fail(fid, "CI red")
    Path(wt, "a.py").write_text("the dead coder's CI fix\n")
    return wt


# ── the ordinary path ────────────────────────────────────────────────────────────────


async def test_salvage_publishes_a_stranded_candidate_and_reviews_it_inline(origin, monkeypatch):
    """The bd-ezs7 shape — a finished `.g1`, the card requeued, then blocked — with the review
    gate on and the PR poll OFF: the reconcile would never pick up `review-pending`, so the
    salvage runs the gate itself, exactly as a drive does."""
    board, gh, loop, fid, reviews = _setup(origin, monkeypatch, review_gate=True)
    cand = await _stranded_candidate(origin, fid)
    board.flag_blocked(fid, "held by the operator")  # blocked over `ready`

    rec = await loop.salvage(fid)

    assert rec["outcome"] == "published", rec
    assert rec["pr_url"] == _URL and rec["draft"] is False and rec["branch"] == _canon(fid)
    assert origin.pushed() == [_canon(fid)] and origin.show(_canon(fid), "adapters.py") == "POLL = 'g1'\n"
    assert len(gh.made("pr", "create")) == 1 and "--draft" not in gh.made("pr", "create")[0]
    assert not os.path.exists(cand)  # promoted to the card's own branch, as a drive's would be
    after = board.get_feature(fid)
    assert after["board_state"] == "in_review" and not after["blocked"] and after["pr_url"] == _URL
    assert reviews == [_URL], "the review gate never ran on the salvaged PR"
    assert loop._inflight_files == {}


async def test_a_red_gate_publishes_nothing_and_hands_back_its_output(origin, monkeypatch):
    board, gh, loop, fid, _ = _setup(origin, monkeypatch, gate=_RED)
    cand = await _stranded_candidate(origin, fid)
    board.flag_blocked(fid, "held")

    rec = await loop.salvage(fid)

    assert rec["outcome"] == "gate-red" and "FAILED tests/test_poll.py::test_it" in rec["gate_output"]
    assert origin.pushed() == [] and gh.calls == [] and board.get_feature(fid)["blocked"]
    assert Path(cand, "README.md").read_text() == "finished work (g1)\n"  # not even promoted


async def test_force_opens_a_red_gate_as_a_draft_that_carries_the_output(origin, monkeypatch):
    board, gh, loop, fid, _ = _setup(origin, monkeypatch, gate=_RED)
    await _stranded_candidate(origin, fid)
    board.flag_blocked(fid, "held")

    rec = await loop.salvage(fid, force=True)

    assert rec["outcome"] == "published" and rec["draft"] is True
    ((*create,),) = gh.made("pr", "create")
    assert "--draft" in create
    body = create[create.index("--body") + 1]
    assert "FAILED tests/test_poll.py::test_it" in body and "no coder was dispatched" in body


# ── force against a PR that already exists, and a repo without drafts ────────────────


async def test_force_on_a_card_with_an_open_pr_drafts_that_pr_and_posts_the_output(origin, monkeypatch):
    """`gh pr create --draft` fails "already exists" and `open_pr` adopts the READY PR: the
    red commit landed on a mergeable PR while the record said "draft". Now the adopted PR is
    converted, the output posted on it, and `draft` is read back from GitHub."""
    board, gh, loop, fid, _ = _setup(origin, monkeypatch, gate=_RED, gh=_Gh(existing=True))
    await _ci_fix_round_died(origin, board, fid)

    rec = await loop.salvage(fid, force=True)

    assert rec["outcome"] == "published" and rec["draft"] is True
    assert gh.made("pr", "ready", _URL, "--undo"), "the existing PR was never converted to a draft"
    assert any("FAILED tests/test_poll.py::test_it" in c for c in gh.comments), "the output never reached the PR"
    assert origin.show(_canon(fid), "a.py") == "the dead coder's CI fix\n"
    assert board.get_feature(fid)["board_state"] == "in_review"


async def test_a_conversion_github_refuses_is_reported_never_assumed(origin, monkeypatch):
    """The same, in a repo without drafts: `gh pr ready --undo` is refused. The PR stays
    mergeable, so the record says so — and the gate output is on the PR regardless."""
    board, gh, loop, fid, _ = _setup(origin, monkeypatch, gate=_RED, gh=_Gh(existing=True, drafts=False))
    await _ci_fix_round_died(origin, board, fid)

    rec = await loop.salvage(fid, force=True)

    assert rec["draft"] is False and "NOT a draft" in rec["detail"]
    assert any("FAILED tests/test_poll.py::test_it" in c for c in gh.comments)


async def test_a_repo_without_drafts_is_reported_truthfully(origin, monkeypatch):
    """A forced salvage in a repo without draft PRs: the push lands, the draft create fails.
    The record names the pushed branch — never a bare "error" over a silently pushed branch."""
    board, gh, loop, fid, _ = _setup(origin, monkeypatch, gate=_RED, gh=_Gh(drafts=False))
    await _stranded_candidate(origin, fid)
    board.flag_blocked(fid, "held")

    rec = await loop.salvage(fid, force=True)

    assert rec["outcome"] == "error" and rec["branch"] == _canon(fid)
    assert f"pushed {_canon(fid)}" in rec["detail"] and "gh pr create --head" in rec["detail"]
    assert origin.pushed() == [_canon(fid)]


async def test_the_gate_output_cannot_break_out_of_its_fence(origin, monkeypatch):
    board, gh, loop, fid, _ = _setup(origin, monkeypatch, gate="printf 'log\\n```\\n# injected heading\\n'; exit 1")
    await _stranded_candidate(origin, fid)
    board.flag_blocked(fid, "held")

    await loop.salvage(fid, force=True)

    ((*create,),) = gh.made("pr", "create")
    block = create[create.index("--body") + 1].split("The pre-PR gate FAILED", 1)[1].splitlines()
    opening = next(ln for ln in block if ln.startswith("`"))
    inside = block[block.index(opening) + 1 :]
    assert len(opening) > 3, "a 3-backtick fence is closed by the ``` the output prints"
    assert "# injected heading" in inside[: inside.index(opening)], "the output escaped its fence"


# ── a card blocked out of review keeps its PR ────────────────────────────────────────


async def test_a_card_blocked_out_of_review_goes_back_to_review_on_its_own_pr(origin, monkeypatch):
    """CI-fix exhaustion and review-gate blocks keep the tree and the PR. Salvaging one used
    to clear the block, fail open_review's in_progress precondition, and report "error"."""
    board, gh, loop, fid, reviews = _setup(origin, monkeypatch, review_gate=True, gh=_Gh(existing=True))
    board.claim(fid, assignee="proto")
    wt = await _canonical_with_pr(origin, fid)
    board.open_review(fid, pr_url=_URL)
    board.flag_blocked(fid, "CI still failing after 3 CI-fix round(s): tests red")
    Path(wt, "a.py").write_text("the operator's hand fix\n")

    rec = await loop.salvage(fid)

    assert rec["outcome"] == "published", rec
    after = board.get_feature(fid)
    assert after["board_state"] == "in_review" and not after["blocked"] and after["pr_url"] == _URL
    assert origin.show(_canon(fid), "a.py") == "the operator's hand fix\n"  # pushed onto its PR
    assert reviews == [_URL]


# ── a cancel during the publish ──────────────────────────────────────────────────────


async def test_a_cancel_during_the_gate_publishes_nothing_and_its_reap_waits(origin, monkeypatch):
    """The cancel route reaps the card's trees. Landing mid-salvage it must neither open a PR
    for a cancelled card nor take the tree out from under the gate — it waits, then saves."""
    board, gh, loop, fid, _ = _setup(origin, monkeypatch)
    cand = await _stranded_candidate(origin, fid)
    board.flag_blocked(fid, "held")
    seen: dict = {}
    real_gate = loop._gate_tree

    async def _gate_with_cancel(f, wt):
        board.cancel_feature(fid, "scope cut")  # …and the cancel route reaps, as api.py does
        seen["reap"] = asyncio.ensure_future(worktree.reap_feature_worktree(origin.clone, ".worktrees", fid))
        done, _ = await asyncio.wait({seen["reap"]}, timeout=2)
        seen["tree_during_gate"] = not done and os.path.exists(wt)
        return await real_gate(f, wt)

    loop._gate_tree = _gate_with_cancel
    rec = await loop.salvage(fid)
    await seen["reap"]

    assert rec["outcome"] == "cancelled" and gh.made("pr", "create") == [] and origin.pushed() == []
    assert seen["tree_during_gate"], "the cancel's reap removed the tree mid-salvage"
    assert not os.path.exists(cand)  # …and once the salvage let go, the reap saved it and removed it
    assert _git("-C", origin.clone, "for-each-ref", "refs/heads/stranded/")


async def test_a_cancel_that_lands_while_the_pr_opens_closes_that_pr(origin, monkeypatch):
    board, gh, loop, fid, _ = _setup(origin, monkeypatch)
    await _stranded_candidate(origin, fid)
    board.flag_blocked(fid, "held")
    real_open = loop._open_tree_pr

    async def _open_then_cancel(*a, **kw):
        url = await real_open(*a, **kw)
        board.cancel_feature(fid, "scope cut")
        return url

    loop._open_tree_pr = _open_then_cancel
    rec = await loop.salvage(fid)

    assert rec["outcome"] == "cancelled" and gh.closed and gh.made("pr", "close", _URL)


# ── the reservation: no claim gets in, and it never drops another holder's ───────────


async def test_the_claim_scan_leaves_a_card_alone_while_it_is_salvaged(origin, monkeypatch):
    board, gh, loop, fid, _ = _setup(origin, monkeypatch)
    await _stranded_candidate(origin, fid)
    board.flag_blocked(fid, "held")
    seen: dict = {}
    real_gate = loop._gate_tree

    async def _gate_with_unblock_and_tick(f, wt):
        board.clear_blocked(fid)  # an unblock lands mid-salvage: the card is `ready` again
        async with loop._claim_guard():
            await loop._spawn_ready()
        seen["claimed"] = live_drive(fid) is not None or board.get_feature(fid)["board_state"] != "ready"
        seen["decision"] = loop._last_claim_decision
        return await real_gate(f, wt)

    loop._gate_tree = _gate_with_unblock_and_tick
    rec = await loop.salvage(fid)

    assert seen["claimed"] is False, "the claim scan claimed a card mid-salvage"
    assert {"fid": fid, "reason": "reserved"} in seen["decision"]["skipped"]  # it was in the queue
    assert rec["outcome"] == "published" and board.get_feature(fid)["board_state"] == "in_review"


async def test_the_reservation_is_released_by_identity_never_another_holders(origin, monkeypatch):
    board, gh, loop, fid, _ = _setup(origin, monkeypatch)
    await _stranded_candidate(origin, fid)
    board.flag_blocked(fid, "held")
    theirs = {("", "a.py")}
    real_gate = loop._gate_tree

    async def _gate_while_another_takes_the_card(f, wt):
        loop._inflight_files[fid] = theirs  # another holder's claim replaces ours mid-salvage
        return await real_gate(f, wt)

    loop._gate_tree = _gate_while_another_takes_the_card
    await loop.salvage(fid)

    assert loop._inflight_files.get(fid) is theirs, "the salvage dropped another holder's claim"


async def test_the_blocked_sweep_leaves_a_reserved_card_alone(origin, monkeypatch):
    board, gh, loop, fid, _ = _setup(origin, monkeypatch)
    board.flag_blocked(fid, "rate limit: 429 too many requests")  # a class the sweep self-heals
    assert board.get_feature(fid)["blocked_class"] == "rate-limit"
    loop._inflight_files[fid] = set()  # a salvage holds it

    await loop._recover_blocked(board)
    assert board.get_feature(fid)["blocked"], "the blocked sweep auto-unblocked a reserved card"

    del loop._inflight_files[fid]  # …and without the reservation it would have: the case is live
    await loop._recover_blocked(board)
    assert not board.get_feature(fid)["blocked"]


async def test_boot_recovery_leaves_a_reserved_card_alone(origin, monkeypatch):
    board, gh, loop, fid, _ = _setup(origin, monkeypatch)
    board.claim(fid, assignee="proto")  # in_progress with no drive — boot recovery's own case
    loop._inflight_files[fid] = set()  # a salvage got there first

    await loop._recover()
    assert board.get_feature(fid)["board_state"] == "in_progress", "boot recovery requeued a reserved card"

    del loop._inflight_files[fid]
    await loop._recover()
    assert board.get_feature(fid)["board_state"] == "ready"


# ── a leftover directory is not a worktree, and git must never be run in it ──────────


async def test_a_leftover_directory_is_never_mistaken_for_the_cards_tree(origin, monkeypatch):
    """A `feat-…` directory with no `.git` of its own sits inside the main checkout: git ran
    in it answers for THAT. It counted the operator's own unpushed commit as the card's
    "changes", ran the gate there, and committed the card's title onto the operator's branch."""
    board, gh, loop, fid, _ = _setup(origin, monkeypatch)
    board.flag_blocked(fid, "held")
    Path(origin.clone, "local.py").write_text("the operator's own work\n")
    _git("-C", origin.clone, "add", "local.py")
    _git("-C", origin.clone, "commit", "-qm", "operator's local commit")
    left = Path(origin.clone, ".worktrees", worktree.worktree_dir(fid, _TITLE))
    left.mkdir(parents=True)
    (left / "stray.txt").write_text("leftover\n")
    head = _git("-C", origin.clone, "rev-parse", "HEAD")

    rec = await loop.salvage(fid)

    assert _git("-C", origin.clone, "rev-parse", "HEAD") == head, "the salvage committed onto the main checkout"
    assert rec["outcome"] == "refused" and "not a worktree of" in rec["detail"]
    assert gh.calls == [] and origin.pushed() == []
    assert await worktree.commits_ahead(str(left), origin.base) == 0  # not the main checkout's 1
    assert await worktree.own_worktree(origin.clone, str(left)) is False


async def test_a_separate_clone_under_the_cards_tree_name_is_not_its_worktree(origin, monkeypatch):
    """Its own top level is not enough: a separate clone dropped under the card's tree name
    answers for itself — with its own uncommitted edits — but the board's repo never
    registered it. Publishing from it would commit into, and push from, somebody else's repo."""
    board, gh, loop, fid, _ = _setup(origin, monkeypatch)
    board.flag_blocked(fid, "held")
    other = Path(origin.clone, ".worktrees", worktree.worktree_dir(fid, _TITLE))
    other.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", origin.origin, str(other))
    _identity(str(other))
    (other / "README.md").write_text("someone else's edit\n")
    head = _git("-C", str(other), "rev-parse", "HEAD")

    rec = await loop.salvage(fid)

    assert _git("-C", str(other), "rev-parse", "HEAD") == head, "the salvage committed into another repo"
    assert rec["outcome"] == "refused" and "not a worktree of" in rec["detail"]
    assert gh.calls == [] and origin.pushed() == []
    assert (other / "README.md").read_text() == "someone else's edit\n"


# ── refusals that change nothing ─────────────────────────────────────────────────────


async def test_salvage_refuses_while_a_drive_or_another_salvage_owns_the_card(origin, monkeypatch):
    board, gh, loop, fid, _ = _setup(origin, monkeypatch, gate="sleep 1")
    cand = await _stranded_candidate(origin, fid)
    board.flag_blocked(fid, "held")

    loop._inflight_files[fid] = {("", "a.py")}  # a live drive's claim
    held = await loop.salvage(fid)
    assert held["outcome"] == "refused" and "live drive" in held["detail"]
    assert Path(cand, "README.md").read_text() == "finished work (g1)\n" and origin.pushed() == []
    del loop._inflight_files[fid]

    first, second = await asyncio.gather(loop.salvage(fid), loop.salvage(fid))
    assert (first["outcome"], second["outcome"]) == ("published", "refused")
    assert len(gh.made("pr", "create")) == 1 and loop._inflight_files == {}


async def test_salvage_refuses_a_card_with_nothing_to_publish(origin, monkeypatch):
    board, gh, loop, fid, _ = _setup(origin, monkeypatch)
    await worktree.create_worktree(origin.clone, origin.base, fid, title=_TITLE)  # a clean canonical tree
    board.flag_blocked(fid, "held")

    rec = await loop.salvage(fid)

    assert rec["outcome"] == "refused" and "has changes vs main" in rec["detail"] and gh.calls == []


async def test_salvage_refuses_a_card_that_is_not_stranded(origin, monkeypatch):
    board, gh, loop, fid, _ = _setup(origin, monkeypatch)
    await _stranded_candidate(origin, fid)
    assert LABEL_READY in board.get_feature(fid)["labels"]

    rec = await loop.salvage(fid)

    assert rec["outcome"] == "refused" and "block it first" in rec["detail"] and gh.calls == []


async def test_several_trees_with_work_need_the_operator_to_pick_one(origin, monkeypatch):
    board, gh, loop, fid, _ = _setup(origin, monkeypatch)
    await _stranded_candidate(origin, fid, "g1", "g1")
    await _stranded_candidate(origin, fid, "g2", "g2")
    board.flag_blocked(fid, "held")

    ambiguous = await loop.salvage(fid)
    assert ambiguous["outcome"] == "refused" and f"feat-{fid}.g1" in ambiguous["detail"]
    assert f"feat-{fid}.g2" in ambiguous["detail"] and gh.calls == []

    picked = await loop.salvage(fid, tree=f"feat-{fid}.g2")
    assert picked["outcome"] == "published" and origin.show(_canon(fid), "adapters.py") == "POLL = 'g2'\n"


async def test_commits_ahead_counts_what_a_pr_would_carry(origin):
    wt, _ = await worktree.create_worktree(origin.clone, origin.base, "bd-7")
    assert await worktree.commits_ahead(wt, origin.base) == 0
    Path(wt, "x.py").write_text("x = 1\n")
    _git("-C", wt, "add", "-A")
    _git("-C", wt, "commit", "-m", "one")
    assert await worktree.commits_ahead(wt, origin.base) == 1


# ── the surfaces: route and tool, over the running loop ──────────────────────────────


async def test_the_route_and_the_tool_carry_the_loops_record(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from project_board import _board_tools, api

    records = {
        "bd-ok": {"outcome": "published", "pr_url": _URL},
        "bd-red": {"outcome": "gate-red", "gate_output": "FAILED"},
        "bd-none": {"outcome": "not-found"},
    }
    asked: list[tuple] = []

    async def _request(fid, *, force=False, tree=""):
        asked.append((fid, force, tree))
        return {"feature_id": fid, **records[fid]}

    monkeypatch.setattr("project_board.loop.request_salvage", _request)
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/project_board")
    client = TestClient(app)
    base = "/api/plugins/project_board/features"
    ok = client.post(f"{base}/bd-ok/salvage", json={"force": True, "tree": "feat-bd-ok.g1"})
    assert ok.status_code == 200 and ok.json()["pr_url"] == _URL
    assert client.post(f"{base}/bd-red/salvage", json={}).status_code == 409
    assert client.post(f"{base}/bd-none/salvage").status_code == 404
    assert asked[0] == ("bd-ok", True, "feat-bd-ok.g1") and asked[1] == ("bd-red", False, "")

    (tool,) = [t for t in _board_tools({}) if t.name == "board_salvage_feature"]
    out = json.loads(await tool.ainvoke({"feature_id": "bd-red", "force": False}))
    assert out["outcome"] == "gate-red"


async def test_with_no_loop_running_there_is_nothing_to_salvage_with(monkeypatch):
    from project_board.loop import request_salvage

    monkeypatch.setattr("project_board.loop.live_loop", lambda: None)
    rec = await request_salvage("bd-7")
    assert rec["outcome"] == "loop-not-running"
