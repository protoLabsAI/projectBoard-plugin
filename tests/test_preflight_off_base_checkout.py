"""Off-base base-checkout recovery (bd-z4jw6) — against REAL git.

``refresh_base_checkout`` fast-forwards a project's main checkout ONLY while it is on its base
branch, so one left on another branch (``/Users/kj/dev/protoAgent-team`` parked on
``prepare-release/v0.242.0`` after a release ship) is never moved and its project's ready cards
sit unclaimed for hours — the gate preflight gets no verdict off base, so the claim scan skips
them as ``preflight-pending`` and nobody is told.

The recovery sweep counts the consecutive passes a checkout is off base and, on the second,
either switches a clean board-owned checkout back (lossless — the branch it leaves keeps its
commits) or pages the operator once. A git failure switches and pages nothing. Everything here
shells real git against a temporary bare origin + clone, like tests/test_base_refresh_452.py —
the whole point of the seam is what git does to a working tree, which no fake can show.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from conftest import REAL_SEAMS

from project_board import health, worktree
from project_board.loop import BoardLoop


@pytest.fixture(autouse=True)
def real_refresh(monkeypatch):
    """conftest stubs the refresh for the unit tier; this tier runs the genuine seam, so a
    switched-back checkout really fast-forwards. ``off_base_checkout``/``switch_base_checkout``
    are not stubbed anywhere, so they always run real git."""
    monkeypatch.setattr(worktree, "refresh_base_checkout", REAL_SEAMS["worktree.refresh_base_checkout"])


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _identity(repo: str) -> None:
    _git("-C", repo, "config", "user.email", "board-test@localhost")
    _git("-C", repo, "config", "user.name", "Board Test")
    _git("-C", repo, "config", "commit.gpgsign", "false")


def _branch(repo: str) -> str:
    return _git("-C", repo, "rev-parse", "--abbrev-ref", "HEAD")


class _Origin:
    base = "main"

    def __init__(self, tmp_path: Path):
        self.origin = str(tmp_path / "origin.git")
        self.seed = str(tmp_path / "seed")
        self.clone = str(tmp_path / "clone")
        _git("init", "--bare", self.origin)
        _git("init", "-b", self.base, self.seed)
        _identity(self.seed)
        Path(self.seed, "README.md").write_text("v0.7.1\n")
        _git("-C", self.seed, "add", "-A")
        _git("-C", self.seed, "commit", "-m", "v0.7.1")
        _git("-C", self.seed, "remote", "add", "origin", self.origin)
        _git("-C", self.seed, "push", "-u", "origin", self.base)
        _git("-C", self.origin, "symbolic-ref", "HEAD", f"refs/heads/{self.base}")
        _git("clone", self.origin, self.clone)
        _identity(self.clone)

    def release(self, text: str = "v0.9.0\n") -> str:
        """Push a new commit to origin's base (a release lands) and return its sha."""
        Path(self.seed, "README.md").write_text(text)
        _git("-C", self.seed, "add", "-A")
        _git("-C", self.seed, "commit", "-m", text.strip())
        _git("-C", self.seed, "push", "origin", self.base)
        return _git("-C", self.seed, "rev-parse", "HEAD")

    def head(self) -> str:
        return _git("-C", self.clone, "rev-parse", "HEAD")

    def park(self, branch: str, *, push: bool, commit: bool = False) -> None:
        """Leave the checkout on another branch, the way a release ship does."""
        _git("-C", self.clone, "switch", "-q", "-c", branch)
        if commit:
            Path(self.clone, "VERSION").write_text("bump\n")
            _git("-C", self.clone, "add", "-A")
            _git("-C", self.clone, "commit", "-q", "-m", "bump")
        if push:
            _git("-C", self.clone, "push", "-q", "-u", "origin", branch)


@pytest.fixture
def origin(tmp_path):
    return _Origin(tmp_path)


def _loop(projects: dict, **extra) -> BoardLoop:
    return BoardLoop({"coder": "proto", "merge_poll": False, "projects": projects, **extra})


class _Ready:
    """A store that only answers ``list_features`` — the held-card read the page makes."""

    def __init__(self, cards):
        self.cards = cards

    def list_features(self, state=None):
        return [dict(c) for c in self.cards]


def _capture_pages(lp):
    pages: list[tuple] = []
    lp._notify_operator = lambda fid, text, *, incident="", store=None: pages.append((fid, text, incident))
    return pages


# ── the worktree seams, directly ──────────────────────────────────────────────────────


async def test_off_base_checkout_reads_on_base(origin):
    off = await worktree.off_base_checkout(origin.clone, "main")
    assert off == {"branch": "main", "off_base": False, "dirty": False, "unpushed": 0, "error": ""}


async def test_off_base_checkout_reports_branch_dirty_and_unpushed(origin):
    origin.park("wip", push=False)
    Path(origin.clone, "local.txt").write_text("mine\n")
    _git("-C", origin.clone, "add", "-A")
    _git("-C", origin.clone, "commit", "-q", "-m", "local only")  # an unpushed commit
    Path(origin.clone, "README.md").write_text("mid edit\n")  # a tracked, uncommitted change
    off = await worktree.off_base_checkout(origin.clone, "main")
    assert off["branch"] == "wip" and off["off_base"] is True
    assert off["dirty"] is True and off["unpushed"] >= 1 and off["error"] == ""


async def test_untracked_files_are_never_dirt(origin):
    origin.park("wip", push=True)
    Path(origin.clone, "scratch.tmp").write_text("junk\n")  # untracked — build output, scratch
    off = await worktree.off_base_checkout(origin.clone, "main")
    assert off["off_base"] is True and off["dirty"] is False and off["unpushed"] == 0 and off["error"] == ""


async def test_a_pushed_release_branch_has_no_unpushed_commits(origin):
    origin.park("prepare-release/v1", push=True, commit=True)
    off = await worktree.off_base_checkout(origin.clone, "main")
    assert off["off_base"] is True and off["dirty"] is False and off["unpushed"] == 0 and off["error"] == ""


async def test_off_base_checkout_on_a_non_repo_is_an_error(tmp_path):
    off = await worktree.off_base_checkout(str(tmp_path / "nope"), "main")
    assert off["off_base"] is None and off["error"]


async def test_switch_base_checkout_moves_to_base_and_keeps_the_branch(origin):
    origin.park("relb", push=True, commit=True)
    tip = origin.head()
    res = await worktree.switch_base_checkout(origin.clone, "main")
    assert res["state"] == "switched"
    assert _branch(origin.clone) == "main"
    # the branch it left is untouched — its commit still exists
    assert _git("-C", origin.clone, "rev-parse", "refs/heads/relb") == tip


async def test_switch_base_checkout_on_a_non_repo_is_unknown_not_an_error(tmp_path):
    res = await worktree.switch_base_checkout(str(tmp_path / "nope"), "main")
    assert res["state"] == "unknown" and res["detail"]


# ── the recovery sweep ─────────────────────────────────────────────────────────────────


async def test_clean_board_owned_off_base_is_switched_back_on_the_second_pass(origin):
    # Parked on a PUSHED release branch (its commit is safe on a remote)…
    origin.park("prepare-release/v1", push=True, commit=True)
    # …while a release the checkout has never seen landed on main.
    new = origin.release()
    lp = _loop({"web": {"repo": origin.clone, "base_refresh": True}})
    pages = _capture_pages(lp)

    await lp._refresh_base_checkouts()  # first pass: nothing moves
    assert _branch(origin.clone) == "prepare-release/v1"
    assert pages == []

    await lp._refresh_base_checkouts()  # second pass: switched back AND fast-forwarded
    assert _branch(origin.clone) == "main"
    assert origin.head() == new
    assert pages == []  # a lossless switch never pages
    assert _git("-C", origin.clone, "rev-parse", "refs/heads/prepare-release/v1")  # branch kept
    # the condition that made the claim scan skip the project is gone
    assert await worktree.base_checkout_dirt(origin.clone, "main") == ""
    assert health.base_checkouts_snapshot()["web"]["state"] in ("fast_forwarded", "current")


async def test_after_recovery_the_preflight_gets_a_verdict_so_claims_resume(origin):
    origin.park("prepare-release/v2", push=True)
    lp = _loop({"web": {"repo": origin.clone, "base_refresh": True}})
    _capture_pages(lp)

    # Off base, the preflight gives NO verdict (dirty/off-base), so the claim scan skips the
    # project's cards as preflight-pending (`_preflight_state[...] is not True`).
    await lp._preflight("web", "true", origin.clone, "main")
    assert lp._preflight_state.get("web") is not True

    await lp._refresh_base_checkouts()
    await lp._refresh_base_checkouts()
    assert _branch(origin.clone) == "main"

    # Back on base, the preflight produces a verdict — the claim scan no longer skips it.
    await lp._preflight("web", "true", origin.clone, "main")
    assert lp._preflight_state.get("web") is True


async def test_a_dirty_off_base_checkout_is_paged_once_and_never_switched(origin):
    origin.park("prepare-release/v1", push=True)
    Path(origin.clone, "README.md").write_text("operator mid-edit\n")  # tracked, uncommitted
    lp = _loop({"web": {"repo": origin.clone, "base_refresh": True}})
    lp._store = lambda: _Ready(
        [{"id": "bd-1", "project": "web"}, {"id": "bd-2", "project": "web"}, {"id": "bd-9", "project": "other"}]
    )
    pages = _capture_pages(lp)

    await lp._refresh_base_checkouts()  # first pass: nothing
    assert pages == [] and _branch(origin.clone) == "prepare-release/v1"

    await lp._refresh_base_checkouts()  # second pass: ONE page
    assert len(pages) == 1
    fid, text, incident = pages[0]
    assert origin.clone in text and "prepare-release/v1" in text and "main" in text
    assert "bd-1" in text and "bd-2" in text and "bd-9" not in text  # only this project's held cards
    assert "uncommitted changes" in text
    assert incident == f"base-checkout:{os.path.realpath(origin.clone)}|prepare-release/v1"

    await lp._refresh_base_checkouts()  # third pass: NOT repeated
    assert len(pages) == 1
    assert _branch(origin.clone) == "prepare-release/v1"  # never switched
    assert Path(origin.clone, "README.md").read_text() == "operator mid-edit\n"  # never discarded
    assert health.base_checkouts_snapshot()["web"]["state"] == "stale"


async def test_an_off_base_branch_with_unpushed_commits_is_paged_not_switched(origin):
    origin.park("wip", push=False)
    Path(origin.clone, "local.txt").write_text("mine\n")
    _git("-C", origin.clone, "add", "-A")
    _git("-C", origin.clone, "commit", "-q", "-m", "local only")
    mine = origin.head()
    lp = _loop({"web": {"repo": origin.clone, "base_refresh": True}})
    pages = _capture_pages(lp)

    await lp._refresh_base_checkouts()
    await lp._refresh_base_checkouts()
    assert len(pages) == 1 and "commits no remote has" in pages[0][1]
    assert _branch(origin.clone) == "wip" and origin.head() == mine  # the commit is preserved


async def test_a_checkout_the_board_does_not_own_is_paged_not_switched(origin):
    origin.park("operator-work", push=False)
    lp = _loop({"mine": {"repo": origin.clone}})  # no base_refresh → not board-owned
    pages = _capture_pages(lp)

    await lp._refresh_base_checkouts()
    await lp._refresh_base_checkouts()
    assert len(pages) == 1 and "not a board-owned checkout" in pages[0][1]
    assert _branch(origin.clone) == "operator-work"
    assert health.base_checkouts_snapshot()["mine"]["state"] == "stale"


async def test_a_git_failure_switches_nothing_and_pages_nothing(origin, monkeypatch):
    origin.park("prepare-release/v1", push=True, commit=True)

    async def _err(repo, base):
        return {"branch": "prepare-release/v1", "off_base": None, "dirty": False, "unpushed": 0, "error": "git blew up"}

    monkeypatch.setattr(worktree, "off_base_checkout", _err)
    lp = _loop({"web": {"repo": origin.clone, "base_refresh": True}})
    pages = _capture_pages(lp)

    await lp._refresh_base_checkouts()
    await lp._refresh_base_checkouts()
    assert pages == []  # uncertain evidence never pages
    assert _branch(origin.clone) == "prepare-release/v1"  # …and never switches


async def test_a_refused_switch_pages_nothing_and_is_retried_next_pass(origin, monkeypatch):
    # The lossless case — a clean, board-owned, fully-pushed off-base checkout — but `git
    # switch` is REFUSED on the pass that first acts. A refused switch is uncertain evidence,
    # the same class as an off-base-check error: it must page NO ONE (AC4). And because a
    # plain `git switch` is idempotent, the board simply retries it on the next pass rather
    # than latching 'acted' and paging — the regression that round 1 rejected.
    origin.park("prepare-release/v1", push=True, commit=True)
    new = origin.release()
    lp = _loop({"web": {"repo": origin.clone, "base_refresh": True}})
    lp._store = lambda: _Ready([{"id": "bd-1", "project": "web"}])  # held cards, in case it paged
    pages = _capture_pages(lp)

    real_switch = worktree.switch_base_checkout
    calls = {"n": 0}

    async def _flaky_switch(repo, base):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"state": "unknown", "detail": "git switch refused (transient)"}
        return await real_switch(repo, base)

    monkeypatch.setattr(worktree, "switch_base_checkout", _flaky_switch)

    await lp._refresh_base_checkouts()  # pass 1: nothing; switch not even attempted
    assert calls["n"] == 0 and pages == [] and _branch(origin.clone) == "prepare-release/v1"

    await lp._refresh_base_checkouts()  # pass 2: acts, switch REFUSED → no page, untouched
    assert calls["n"] == 1
    assert pages == []  # a refused switch never pages
    assert _branch(origin.clone) == "prepare-release/v1"  # left exactly as it was
    assert health.base_checkouts_snapshot()["web"]["state"] == "stale"  # still visible on /status

    await lp._refresh_base_checkouts()  # pass 3: RETRIED (not latched) → switched + fast-forwarded
    assert calls["n"] == 2
    assert pages == []
    assert _branch(origin.clone) == "main"
    assert origin.head() == new


async def test_a_running_gate_smoke_defers_the_switch_and_pages_nothing(origin):
    # A `git switch` is a checkout mutation, so it must never run under a registration gate
    # smoke (#452 review M2) — the same lock `_refresh_one` honours. While the smoke holds the
    # checkout's lock the lossless switch is deferred, not forced and not paged; once the smoke
    # releases, the next pass switches it back.
    from project_board.project_registry import _SMOKE_LOCKS

    origin.park("prepare-release/v1", push=True, commit=True)
    new = origin.release()
    lp = _loop({"web": {"repo": origin.clone, "base_refresh": True}})
    pages = _capture_pages(lp)

    lock = _SMOKE_LOCKS.setdefault(str(Path(origin.clone).expanduser().resolve()), asyncio.Lock())
    await lock.acquire()  # a registration gate smoke is running in the checkout right now
    try:
        await lp._refresh_base_checkouts()  # pass 1
        await lp._refresh_base_checkouts()  # pass 2: would switch, but the smoke holds the lock
        assert _branch(origin.clone) == "prepare-release/v1"  # deferred, not switched
        assert pages == []  # a lossless case is never an operator page
    finally:
        lock.release()

    await lp._refresh_base_checkouts()  # smoke done: the switch is retried and succeeds
    assert _branch(origin.clone) == "main" and origin.head() == new and pages == []


async def test_a_branch_change_resets_the_consecutive_count(origin):
    origin.park("prepare-release/v1", push=True, commit=True)
    lp = _loop({"web": {"repo": origin.clone, "base_refresh": True}})
    pages = _capture_pages(lp)

    await lp._refresh_base_checkouts()  # v1, pass 1
    # the operator moves to a different branch before the second pass
    _git("-C", origin.clone, "switch", "-q", "main")
    origin.park("prepare-release/v2", push=True, commit=True)
    await lp._refresh_base_checkouts()  # v2, pass 1 again (count reset by the branch change)
    assert pages == []
    assert _branch(origin.clone) == "prepare-release/v2"  # not switched on only the first pass for v2
