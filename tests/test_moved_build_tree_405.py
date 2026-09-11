"""A build that stands aside keeps its tree, and whatever removes that tree saves it (#398 × #405).

The two fixes meet here. #398: a drive whose card was moved on under it (a human hold, a
done, a requeue) stands aside. It blocks nothing, and a card held before its PR opens gets
no PR, so the finished work stays unpushed in its worktree. #405: no edge removes a tree
holding work without first saving that work to a `stranded/…` branch.

So a held card's finished build must survive every edge that can end its tree:

- the by-id reap (merge, cancel, done, the health sweep);
- the card's next fresh build, which clears the ground first;
- shutdown, which must not touch a tree the drive no longer holds.

Each of those ends it with the work saved, and none of them touches the card the human held.

Real git (a bare origin and a clone; the worktree and the saved branch are real) and a REAL
`br` board. Only the coder's dispatch and `gh` are faked.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import project_board.loop as loop_mod
import project_board.loop.drive as drive_mod
from project_board import store as store_mod
from project_board import worktree
from project_board.failures import Policy
from project_board.loop import BoardLoop
from project_board.store import BeadsBoard

pytestmark = pytest.mark.skipif(
    shutil.which(store_mod.BR) is None,
    reason="real `br` (beads) CLI not on PATH (CI installs it and sets PB_REQUIRE_BR=1)",
)

_TITLE = "Make the poll timeout progress-based"
_URL = "https://github.com/o/r/pull/9"
_HOLD = "held for QA by a human — do not ship yet"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _identity(repo: str) -> None:
    _git("-C", repo, "config", "user.email", "board-test@localhost")
    _git("-C", repo, "config", "user.name", "Board Test")
    _git("-C", repo, "config", "commit.gpgsign", "false")


class _Gh:
    """`gh`, faked: no PR exists until one is created, and every call is recorded."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.created = False

    async def __call__(self, *args, cwd, timeout=60):
        self.calls.append(args)
        if args[:2] == ("pr", "create"):
            self.created = True
            return 0, _URL + "\n", ""
        if args[:2] == ("pr", "view") and "url" in args:
            return (0, _URL + "\n", "") if self.created else (1, "", "no pull requests found")
        return 1, "", f"unexpected gh call {args}"


class _Card:
    """One card on a real board over a real clone, and a loop whose coder writes a finished
    implementation into the real worktree, then waits to be released."""

    def __init__(self, tmp_path: Path, monkeypatch):
        origin, seed, self.clone = (str(tmp_path / n) for n in ("origin.git", "seed", "clone"))
        _git("init", "--bare", origin)
        _git("init", "-b", "main", seed)
        _identity(seed)
        Path(seed, "target.py").write_text("x = 1\n")
        Path(seed, ".gitignore").write_text(".beads/\n")
        _git("-C", seed, "add", "-A")
        _git("-C", seed, "commit", "-m", "base")
        _git("-C", seed, "remote", "add", "origin", origin)
        _git("-C", seed, "push", "-u", "origin", "main")
        _git("-C", origin, "symbolic-ref", "HEAD", "refs/heads/main")
        _git("clone", origin, self.clone)
        _identity(self.clone)
        self.origin = origin

        self.board = BeadsBoard(repo=self.clone, actor="test")
        monkeypatch.setattr(loop_mod, "get_store", lambda **_kw: self.board)
        monkeypatch.setattr(store_mod, "get_store", lambda **_kw: self.board)
        loop_mod._PENDING_FEEDBACK.clear()
        f = self.board.create_feature(
            _TITLE, spec="s", acceptance_criteria="- WHEN x THE SYSTEM SHALL y", files_to_modify=["target.py"]
        )
        self.fid = f["id"]
        self.board.mark_ready(self.fid)

        self.gh = _Gh()
        monkeypatch.setattr(worktree, "_gh", self.gh)
        self.rounds: list[tuple[asyncio.Event, asyncio.Event, str]] = []

        async def _dispatch(c, wt, prompt, *, timeout=None, env_passthrough=()):
            started, release, content = self.rounds[len(self.dispatched)]
            self.dispatched.append(wt)
            Path(wt, "target.py").write_text(content)
            Path(wt, "poll.py").write_text("PROGRESS_BASED = True\n")
            started.set()
            await release.wait()  # the coder is still working
            # A finished build: it disposes of the card's one requirement, so the drive's next
            # edge is the PR — not a ledger follow-up.
            return "## Summary\n\n- made the poll timeout progress-based\n\n## Requirements\n\n- r1: done\n"

        self.dispatched: list[str] = []
        monkeypatch.setattr(worktree, "dispatch_coder", _dispatch)
        self.loop = BoardLoop({"coder": "proto", "repo": self.clone})
        monkeypatch.setattr(self.loop, "_resolve_delegate", lambda name, expect: object())
        self.tree = os.path.join(self.clone, ".worktrees", worktree.worktree_dir(self.fid, _TITLE))

    async def start_round(self, content: str) -> asyncio.Task:
        started, release = asyncio.Event(), asyncio.Event()
        self.rounds.append((started, release, content))
        assert await self.loop._spawn_ready(), "the loop claimed nothing"
        drive = next(t for t in self.loop._drives if not t.done())
        await asyncio.wait_for(started.wait(), 20)
        return drive

    async def finish_round(self, drive: asyncio.Task) -> None:
        self.rounds[-1][1].set()
        await asyncio.wait_for(drive, 60)
        await asyncio.sleep(0)  # the done-callback releases the card's claim

    def stranded(self) -> list[str]:
        out = _git("-C", self.clone, "for-each-ref", "--format=%(refname:short)", "refs/heads/stranded/")
        return [r for r in out.splitlines() if r]

    def pushed(self) -> list[str]:
        out = _git("-C", self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/feat/")
        return [r for r in out.splitlines() if r]

    def card(self) -> dict:
        return self.board.get_feature(self.fid)


async def _held_mid_build(card: _Card) -> dict:
    """The first round builds; a human holds the card while its coder works; the round ends."""
    drive = await card.start_round("x = 2  # the finished implementation\n")
    card.board.flag_blocked(card.fid, _HOLD)
    held = card.card()
    await card.finish_round(drive)
    return held


def _unchanged(card: _Card, held: dict) -> None:
    now = card.card()
    assert now["board_state"] == held["board_state"] == "blocked"
    assert now["blocked_reason"] == held["blocked_reason"] and _HOLD in now["blocked_reason"]
    assert now["blocked_class"] == held["blocked_class"]
    assert not now.get("pr_url")


async def test_a_held_builds_tree_is_saved_when_the_reap_ends_it(tmp_path, monkeypatch):
    card = _Card(tmp_path, monkeypatch)
    held = await _held_mid_build(card)

    # #398: the finished build stood aside at its PR edge. No PR, nothing pushed, the card
    # still the human's, and the finished work still in the tree.
    assert any("opened no PR for a card that is no longer its own" in c for c in card.board.feature_comments(card.fid))
    assert not card.gh.created and card.pushed() == []
    _unchanged(card, held)
    assert Path(card.tree, "target.py").read_text() == "x = 2  # the finished implementation\n"

    # Shutdown does not own a tree the drive let go of: it must not remove it, saved or not.
    await card.loop.stop()
    assert os.path.isdir(card.tree), "shutdown removed the tree of a build that stood aside"
    assert card.stranded() == []

    # The by-id reap (merge, cancel, done, the health sweep) ends the tree — saving it first.
    await worktree.reap_feature_worktree(card.clone, ".worktrees", card.fid)

    assert not os.path.exists(card.tree)
    (ref,) = card.stranded()
    assert _git("-C", card.clone, "show", f"{ref}:target.py") == "x = 2  # the finished implementation"
    assert _git("-C", card.clone, "show", f"{ref}:poll.py") == "PROGRESS_BASED = True"
    _unchanged(card, held)  # the reap saved and removed the tree; the card is still the human's


async def test_a_held_builds_tree_is_saved_when_the_next_build_clears_the_ground(tmp_path, monkeypatch):
    """The hold is lifted, the sweep hands the card back to the queue, and the next round
    starts fresh. Its first step clears the card's old tree — saving the first round's work."""
    card = _Card(tmp_path, monkeypatch)
    await _held_mid_build(card)
    assert os.path.isdir(card.tree) and card.stranded() == []

    card.board.clear_blocked(card.fid)  # the human lifts the hold: in_progress, no drive
    await card.loop._reconcile_orphan(card.fid)  # the sweep requeues it
    assert card.card()["board_state"] == "ready"

    drive = await card.start_round("x = 3  # the second round\n")

    (ref,) = card.stranded()
    assert _git("-C", card.clone, "show", f"{ref}:target.py") == "x = 2  # the finished implementation"
    assert any(ref in c for c in card.board.feature_comments(card.fid)), "the card never learned where it went"
    assert Path(card.tree, "target.py").read_text() == "x = 3  # the second round\n"  # a fresh tree

    await card.finish_round(drive)
    assert card.card()["board_state"] == "in_review"


async def test_a_hold_landing_while_a_retry_is_prepared_keeps_the_failed_attempts_tree(tmp_path, monkeypatch):
    """A retry throws the drive's own failed attempt away before it rebuilds: that is its own
    judged work, and it is not saved (#405). A hold that lands after the retry's ownership
    check, while its prompt is built, makes that tree a moved build's. It is kept, not
    thrown away."""
    card = _Card(tmp_path, monkeypatch)
    dispatches: list[str] = []

    async def _provider_falls_over(c, wt, prompt, *, timeout=None, env_passthrough=()):
        dispatches.append(wt)
        Path(wt, "target.py").write_text(f"x = 2  # attempt {len(dispatches)}, half done\n")
        raise worktree.WorktreeError("coder dispatch failed: 502 Bad Gateway")

    real_classify = drive_mod.classify

    def _no_backoff(text, **kw):  # the transient retry, minus its 15s sleep
        return Policy("transient", True, 0.0, 3) if "502" in text else real_classify(text, **kw)

    prepared = 0

    async def _lessons(feature):
        nonlocal prepared
        prepared += 1
        if prepared == 2:  # the retry's prompt is being built: its ownership check has passed
            card.board.flag_blocked(card.fid, _HOLD)
        return ""

    monkeypatch.setattr(worktree, "dispatch_coder", _provider_falls_over)
    monkeypatch.setattr(drive_mod, "classify", _no_backoff)
    monkeypatch.setattr(card.loop, "_fetch_kg_lessons", _lessons)

    assert await card.loop._spawn_ready()
    await asyncio.wait_for(next(iter(card.loop._drives)), 60)

    assert len(dispatches) == 1, "the retry was dispatched on a held card"
    assert Path(card.tree, "target.py").read_text() == "x = 2  # attempt 1, half done\n", "the tree was thrown away"
    assert _HOLD in card.card()["blocked_reason"]
