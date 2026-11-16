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
from unittest.mock import AsyncMock, Mock

import pytest

from project_board import work_locations, worktree
from project_board.store import BoardTimeout
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


@pytest.mark.parametrize(
    ("pushed", "has_sha", "retires"),
    [(False, True, False), (False, False, True), (True, True, True), (None, True, True)],
)
async def test_missing_tree_retirement_preserves_unpushed_recovery(tmp_path, pushed, has_sha, retires):
    repo = _Repo(tmp_path)
    tree = repo.make_tree()
    Path(tree, "unique.txt").write_text("unpublished work\n")
    _git("-C", tree, "add", "unique.txt")
    _git("-C", tree, "commit", "-m", "unpublished work")
    sha = _git("-C", tree, "rev-parse", "HEAD")
    store = FakeStore(
        locations=[
            {
                "id": "tree",
                "path": tree,
                "purpose": "unpublished work",
                "pushed": pushed,
                "sha": sha if has_sha else "",
            }
        ]
    )
    original = deepcopy(store._locations["tree"])
    # Simulate removal outside the save-before-reap path, before merge is observed.
    _git("-C", repo.clone, "worktree", "remove", tree)
    loop = _loop(repo)
    await loop._reconcile_pr(store, _merged(store), known_state="MERGED")
    assert store.feature["board_state"] == "done"
    assert bool(store._locations["tree"].get("retired")) is retires

    # A fresh loop retry (as used by health sweeps) must preserve the same policy.
    await _loop(repo)._retire_merged_work_locations(store, FID, repo.clone)
    entry = store._locations["tree"]
    assert work_locations.skip_recovery(entry, "done") is retires
    if not retires:
        assert entry == original
        # A subsequent affirmative publication report permits retirement on retry.
        store.record_work_location(FID, {**entry, "pushed": True})
        await loop._retire_merged_work_locations(store, FID, repo.clone)
        assert store._locations["tree"]["retired"] is True


async def test_deferred_merge_is_retired_by_orphan_sweep(tmp_path, monkeypatch):
    repo = _Repo(tmp_path)
    tree = repo.make_tree()
    store = FakeStore(locations=[{"id": "tree", "path": tree, "purpose": "build"}])
    loop = _loop(repo)
    monkeypatch.setattr(worktree, "processes_in_trees", AsyncMock(return_value={}))
    drive = asyncio.create_task(asyncio.Event().wait())
    _drive_slot().drives[FID] = drive
    try:
        await loop._reconcile_pr(store, _merged(store), known_state="MERGED")
        assert store.feature["board_state"] == "done"
        assert "retired" not in store._locations["tree"]
    finally:
        drive.cancel()
        await asyncio.gather(drive, return_exceptions=True)
        _drive_slot().drives.pop(FID, None)

    # No second merge poll: done cards leave that lane. The orphan sweep owns retry.
    await loop._sweep_worktrees(store, repo.clone)
    assert not os.path.exists(tree)
    assert store._locations["tree"]["retired"] is True


async def test_kept_candidate_locations_remain_active_until_candidate_reaped(tmp_path, monkeypatch):
    repo = _Repo(tmp_path)
    tree = repo.make_tree()
    candidate = repo.make_tree(FID + ".g1")
    Path(candidate, "unique.txt").write_text("sole copy of work")
    store = FakeStore(
        locations=[
            {"id": "canonical", "path": tree, "purpose": "build"},
            {"id": "candidate", "path": candidate + "/missing-output", "purpose": "candidate"},
        ]
    )
    loop = _loop(repo)
    discard = worktree.discard_worktree

    async def cannot_save(repo, path, branch, **kwargs):
        if path == candidate:
            return False, worktree.StrandedTree(path, branch, "save failed")
        return await discard(repo, path, branch, **kwargs)

    monkeypatch.setattr(worktree, "discard_worktree", cannot_save)
    await loop._reconcile_pr(store, _merged(store), known_state="MERGED")
    assert not os.path.exists(tree)
    assert Path(candidate, "unique.txt").read_text() == "sole copy of work"
    assert store._locations["canonical"]["retired"] is True
    assert "retired" not in store._locations["candidate"]

    monkeypatch.setattr(worktree, "discard_worktree", discard)
    monkeypatch.setattr(worktree, "processes_in_trees", AsyncMock(return_value={}))
    await loop._sweep_worktrees(store, repo.clone)
    assert not os.path.exists(candidate)
    assert store._locations["candidate"]["retired"] is True


async def test_health_sweep_retries_retirement_after_tree_already_gone(tmp_path, monkeypatch):
    repo = _Repo(tmp_path)
    tree = repo.make_tree()
    store = FakeStore(locations=[{"id": "tree", "path": tree, "purpose": "build"}], fail_record_for={"tree"})
    loop = _loop(repo)
    await loop._reconcile_pr(store, _merged(store), known_state="MERGED")
    assert not os.path.exists(tree)
    assert "retired" not in store._locations["tree"]
    store.fail_record_for.clear()

    # A new loop proves retry is durable, with no pending-reap memory or on-disk tree.
    loop = _loop(repo)
    monkeypatch.setattr(loop, "_store", lambda: store)
    monkeypatch.setattr(loop, "_all_repos", lambda: [repo.clone])
    monkeypatch.setattr(
        store, "list_features", lambda *, state: [store.feature] if state == "done" else [], raising=False
    )
    monkeypatch.setattr(store, "archive_stale", lambda days: 0, raising=False)
    monkeypatch.setattr(loop, "_start_base_refresh", lambda: None)
    monkeypatch.setattr(loop, "_publish_orphaned_cards", AsyncMock())
    monkeypatch.setattr(loop, "_recover_blocked", AsyncMock())
    monkeypatch.setattr(loop, "_publish_work_snapshot", AsyncMock())
    await loop._sweep()
    assert store._locations["tree"]["retired"] is True


def test_recorded_path_matches_only_feature_tree_names(tmp_path):
    base = tmp_path / ".worktrees"
    for name in (f"feat-{FID}", f"feat-{FID}-slug", f"feat-{FID}.g1", f"feat-{FID}.test.g2"):
        tree = str(base / name)
        assert worktree.feature_tree_for_path(str(tmp_path), ".worktrees", FID, tree + "/file") == tree
    for name in (f"feat-{FID}2", f"feat-{FID}.gx", f"feat-{FID}.other", "elsewhere"):
        assert worktree.feature_tree_for_path(str(tmp_path), ".worktrees", FID, str(base / name)) is None
    assert worktree.feature_tree_for_path(str(tmp_path), ".worktrees", FID, str(tmp_path / f"feat-{FID}")) is None


async def test_candidate_reap_checks_parent_drive(tmp_path):
    repo = _Repo(tmp_path)
    candidate = repo.make_tree(FID + ".c1")
    loop = _loop(repo)
    drive = asyncio.create_task(asyncio.Event().wait())
    _drive_slot().drives[FID] = drive
    try:
        assert not await loop._reap_feature_worktree(repo.clone, FID + ".c1")
        assert os.path.isdir(candidate)
    finally:
        drive.cancel()
        await asyncio.gather(drive, return_exceptions=True)
        _drive_slot().drives.pop(FID, None)


@pytest.mark.parametrize("reap_succeeds", [False, True])
async def test_auto_merge_deletes_remote_branch_only_after_successful_reap(tmp_path, monkeypatch, reap_succeeds):
    repo = _Repo(tmp_path)
    store = FakeStore()
    loop = _loop(repo)
    monkeypatch.setattr(loop, "_auto_merge_blockers", AsyncMock(return_value=[]))
    monkeypatch.setattr(worktree, "merge_pr", AsyncMock(return_value=(True, "")))
    events = []

    async def reap(*args):
        events.append("reap")
        return reap_succeeds

    async def delete(*args):
        events.append("delete")
        return True

    monkeypatch.setattr(loop, "_reap_feature_worktree", reap)
    monkeypatch.setattr(worktree, "delete_remote_branch", delete)
    assert await loop._maybe_auto_merge(store, FID, PR, repo.clone) is True
    assert events == (["reap", "delete"] if reap_succeeds else ["reap"])


async def test_drive_starting_during_auto_merge_keeps_tree_and_published_ref(tmp_path, monkeypatch):
    repo = _Repo(tmp_path)
    tree = repo.make_tree()
    branch = worktree.branch_name(FID)
    _git("-C", tree, "push", "-u", "origin", branch)
    store = FakeStore()
    loop = _loop(repo)
    monkeypatch.setattr(loop, "_auto_merge_blockers", AsyncMock(return_value=[]))
    drive = None

    async def merge(*args, **kwargs):
        nonlocal drive
        # The pre-merge liveness check passed; a drive arrives during the GitHub call.
        drive = asyncio.create_task(asyncio.Event().wait())
        _drive_slot().drives[FID] = drive
        return True, ""

    monkeypatch.setattr(worktree, "merge_pr", merge)
    try:
        assert await loop._maybe_auto_merge(store, FID, PR, repo.clone) is True
        assert os.path.isdir(tree)
        assert _git("-C", repo.clone, "rev-parse", f"refs/remotes/origin/{branch}")
        assert _git("-C", repo.origin, "rev-parse", f"refs/heads/{branch}")
    finally:
        if drive is not None:
            drive.cancel()
            await asyncio.gather(drive, return_exceptions=True)
        _drive_slot().drives.pop(FID, None)

    # The normal MERGED poll can now reap, with the published ref still available.
    await loop._reconcile_pr(store, _merged(store), known_state="MERGED")
    assert store.feature["board_state"] == "done"
    assert not os.path.exists(tree)


@pytest.mark.parametrize("operation", ["list_work_locations", "record_work_location"])
@pytest.mark.parametrize("orphan", [False, True])
async def test_retirement_timeout_stops_sweep_after_first_store_stall(tmp_path, monkeypatch, operation, orphan):
    repo = _Repo(tmp_path)
    tree = repo.make_tree() if orphan else os.path.join(repo.clone, repo.root, f"feat-{FID}")
    store = FakeStore(locations=[
        {"id": "first", "path": tree, "purpose": "build"},
        {"id": "second", "path": tree + "/output", "purpose": "output"},
    ])
    store.feature["board_state"] = "done"
    loop = _loop(repo)
    monkeypatch.setattr(loop, "_store", lambda: store)
    monkeypatch.setattr(loop, "_all_repos", lambda: [repo.clone])
    monkeypatch.setattr(worktree, "processes_in_trees", AsyncMock(return_value={}))
    cards = [store.feature, {**store.feature, "id": "bd-next"}]
    monkeypatch.setattr(store, "list_features", lambda *, state: cards if state == "done" else [], raising=False)
    reads = Mock(wraps=store.list_work_locations)
    writes = Mock(wraps=store.record_work_location)
    monkeypatch.setattr(store, "list_work_locations", reads)
    monkeypatch.setattr(store, "record_work_location", writes)
    stalled = reads if operation == "list_work_locations" else writes
    stalled.side_effect = BoardTimeout("store stalled")
    later_phase = Mock()
    monkeypatch.setattr(loop, "_start_base_refresh", later_phase)

    with pytest.raises(BoardTimeout, match="store stalled"):
        await loop._sweep()

    reads.assert_called_once_with(FID)
    assert stalled.call_count == 1
    assert writes.call_count == (operation == "record_work_location")
    later_phase.assert_not_called()


@pytest.mark.parametrize("operation", ["list_work_locations", "record_work_location"])
async def test_retirement_timeout_remains_best_effort_at_merge_close(tmp_path, monkeypatch, operation):
    repo = _Repo(tmp_path)
    tree = repo.make_tree()
    store = FakeStore(locations=[{"id": "tree", "path": tree, "purpose": "build"}])
    monkeypatch.setattr(store, operation, Mock(side_effect=BoardTimeout("store stalled")))

    await _loop(repo)._reconcile_pr(store, _merged(store), known_state="MERGED")

    assert store.feature["board_state"] == "done"
    assert store.cleared
    assert not os.path.exists(tree)
