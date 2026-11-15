"""A reconcile reap never removes a worktree a LIVE drive owns, and a merged card
retires the work locations under the tree its close reaped (#752).

Two defects from #752 (mythxEngineer, 2026-11-13):

1. A worktree vanished under a live drive (friction f53268e9). ``reap_feature_worktree``
   is called from many reconcile paths with no check that a drive still holds the tree, so
   a reconcile pass tore a running coder's tree out from under it. Every reap site now goes
   through ``ReconcileMixin._reap_feature_worktree``, which defers the reap while the loop
   holds a live drive for the card (the SAME in-flight drive registry
   ``board_requeue_feature`` refuses on).

2. After a card's PR merged and its tree was reaped, its recorded work locations still
   pointed at the deleted paths, so the sweep read ``[recovery] Recorded work needs
   recovery`` forever. The merge edge now retires the entries that pointed into the reaped
   tree (``retired: true``), leaving entries elsewhere untouched.

The reap itself is REAL git here (a bare origin + a clone + a real ``feat-<id>`` worktree):
the defect is in whether the reconcile removes a live tree, which a faked reap can't show.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

from project_board import work_locations, worktree
from project_board.loop import BoardLoop, _drive_slot


FID = "bd-752"
PR = "https://github.com/acme/widgets/pull/7"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _identity(repo: str) -> None:
    _git("-C", repo, "config", "user.email", "board-test@localhost")
    _git("-C", repo, "config", "user.name", "Board Test")
    _git("-C", repo, "config", "commit.gpgsign", "false")


class _Repo:
    """A bare origin + a clone, so ``reap_feature_worktree`` resolves ``origin/<base>`` as
    in production and really removes a clean tree."""

    base = "main"

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.origin = str(tmp_path / "origin.git")
        self.seed = str(tmp_path / "seed")
        self.clone = str(tmp_path / "clone")
        _git("init", "--bare", self.origin)
        _git("init", "-b", self.base, self.seed)
        _identity(self.seed)
        Path(self.seed, ".gitignore").write_text(".worktrees/\n")
        Path(self.seed, "README.md").write_text("base\n")
        _git("-C", self.seed, "add", "-A")
        _git("-C", self.seed, "commit", "-m", "base commit")
        _git("-C", self.seed, "remote", "add", "origin", self.origin)
        _git("-C", self.seed, "push", "-u", "origin", self.base)
        _git("-C", self.origin, "symbolic-ref", "HEAD", f"refs/heads/{self.base}")
        _git("clone", self.origin, self.clone)
        _identity(self.clone)
        self.root = ".worktrees"

    def make_tree(self, fid: str = FID) -> str:
        """A real ``feat-<fid>`` worktree at ``origin/main`` with no work of its own — so a
        reap removes it cleanly and nothing is stranded."""
        path = os.path.join(self.clone, self.root, f"feat-{fid}")
        _git("-C", self.clone, "worktree", "add", "-b", f"feat/{fid}", path, f"origin/{self.base}")
        return path


class FakeStore:
    """The slim store surface ``_reconcile_pr``'s MERGED / CLOSED edges touch, plus an
    in-memory work-location ledger that honours ``retired``."""

    def __init__(self, *, locations=None, fail_record_for=None):
        self.feature = {"id": FID, "pr_url": PR, "board_state": "in_review", "labels": ["in-review"]}
        self._locations: dict[str, dict] = {}
        for entry in locations or []:
            state = work_locations.validate(entry)
            self._locations[state["id"]] = state
        self.fail_record_for = set(fail_record_for or ())
        self.cleared: list = []
        self.blocked: list = []

    def record_merge(self, *, pr_url):
        assert pr_url == PR
        self.feature["board_state"] = "done"
        return deepcopy(self.feature)

    def get_feature(self, fid):
        return deepcopy(self.feature)

    def clear_budgets(self, fid, kinds=None):
        self.cleared.append((fid, kinds))
        return deepcopy(self.feature)

    def flag_blocked(self, fid, reason, category="", *, automatic=False):
        self.feature["board_state"] = "blocked"
        self.blocked.append(reason)
        return deepcopy(self.feature)

    def list_work_locations(self, fid):
        return {
            "version": 1,
            "entries": [deepcopy(e) for e in self._locations.values()],
            "history": [],
            "errors": [],
        }

    def record_work_location(self, fid, entry):
        state = work_locations.validate(entry)
        if state["id"] in self.fail_record_for:
            raise RuntimeError("record_work_location failed")
        self._locations[state["id"]] = state
        return self.list_work_locations(fid)


def _loop(repo: _Repo) -> BoardLoop:
    loop = BoardLoop({})
    loop._repo_for = lambda f: repo.clone  # type: ignore[assignment]
    assert loop.root == repo.root
    return loop


def _merged(store: FakeStore) -> dict:
    return {**deepcopy(store.feature), "board_state": "in_review"}


# ── r1: a live drive keeps its tree; the next pass after it ends reaps it ────────────────


async def test_reconcile_defers_reap_while_a_drive_is_live_then_reaps(tmp_path):
    repo = _Repo(tmp_path)
    tree = repo.make_tree()
    store = FakeStore()
    loop = _loop(repo)

    ev = asyncio.Event()
    drive = asyncio.create_task(ev.wait())
    _drive_slot().drives[FID] = drive
    try:
        # A reconcile reap while the drive still owns the tree must leave it in place.
        await loop._reconcile_pr(store, _merged(store), known_state="MERGED")
        assert os.path.isdir(tree), "a live drive's worktree must survive the reconcile reap (fails on main)"

        # The drive ends → a later pass reaps the now-unowned tree.
        ev.set()
        await drive
        await loop._reconcile_pr(store, _merged(store), known_state="MERGED")
        assert not os.path.exists(tree), "once the drive ends the reap removes the tree"
    finally:
        _drive_slot().drives.pop(FID, None)
        if not drive.done():
            drive.cancel()


# ── r2: every reap site routes through the guard ─────────────────────────────────────────


async def test_guard_refusal_stops_every_reap_site(tmp_path, monkeypatch):
    repo = _Repo(tmp_path)
    store = FakeStore()
    loop = _loop(repo)
    reap = AsyncMock(return_value=True)
    monkeypatch.setattr(worktree, "reap_feature_worktree", reap)
    monkeypatch.setattr(loop, "_flag_blocked", lambda *a, **k: None)
    # The guard refuses: no reconcile reap site may touch the tree.
    monkeypatch.setattr(loop, "_reap_held_by_drive", lambda fid: True)

    await loop._reconcile_pr(store, _merged(store), known_state="MERGED")  # merge-poll path
    await loop._reconcile_pr(store, _merged(store), known_state="CLOSED")  # cancel path

    reap.assert_not_awaited()


def test_no_reconcile_reap_site_bypasses_the_guard():
    """Structural guard (#752): every reap in reconcile.py goes through the single wrapper,
    so a future edit can't add a site that skips the live-drive check."""
    src = (Path(__file__).resolve().parent.parent / "loop" / "reconcile.py").read_text()
    direct = re.findall(r"worktree\.reap_feature_worktree\(", src)
    assert len(direct) == 1, f"expected exactly one direct reap (the wrapper); found {len(direct)}"
    assert "return await worktree.reap_feature_worktree(repo, self.root, fid)" in src


# ── r3: a merged card retires the work locations under its reaped tree ───────────────────


async def test_merge_retires_work_locations_under_the_reaped_tree(tmp_path):
    repo = _Repo(tmp_path)
    tree = repo.make_tree()
    inside = os.path.join(tree, "sub", "scratch.py")
    outside = str(tmp_path / "elsewhere" / "notes.md")
    store = FakeStore(
        locations=[
            {"id": "intree", "path": inside, "purpose": "build output in the worktree"},
            {"id": "elsewhere", "path": outside, "purpose": "a scratch repo elsewhere"},
        ]
    )
    loop = _loop(repo)

    await loop._reconcile_pr(store, _merged(store), known_state="MERGED")

    assert not os.path.exists(tree)  # really reaped (no live drive)
    assert store._locations["intree"].get("retired") is True
    assert "retired" not in store._locations["elsewhere"]  # an entry outside the tree is untouched


# ── r4: a retire failure never blocks the close ─────────────────────────────────────────


async def test_retire_failure_never_blocks_the_close(tmp_path):
    repo = _Repo(tmp_path)
    tree = repo.make_tree()
    inside = os.path.join(tree, "scratch.py")
    store = FakeStore(
        locations=[{"id": "intree", "path": inside, "purpose": "x"}],
        fail_record_for={"intree"},
    )
    loop = _loop(repo)

    # Must NOT raise: the card still closes and its tree is still reaped.
    await loop._reconcile_pr(store, _merged(store), known_state="MERGED")

    assert store.feature["board_state"] == "done"
    assert store.cleared, "budgets reset → the close completed past the failed retire"
    assert not os.path.exists(tree)
    assert "retired" not in store._locations["intree"]  # the failed re-record left it as it was
