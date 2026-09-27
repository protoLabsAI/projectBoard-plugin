"""#461: the health sweep reaped LIVE worktrees on any board whose bead prefix is not `bd-`.

`worktree._FID_PREFIX_RE` hard-coded `bd-`. On the designSystem board (ids `ds-…`) the dir
`feat-ds-vvi-plugin-hardening-…` parsed to the id `ds-vvi-plugin-hardening-…`, the store
had no such card, and `_sweep_worktrees` reaped the tree as an orphan — every sweep, for
every slugged tree on the board: a drive about to run its pre-PR gate, a card in review
waiting on its merge gate, a coder stalled mid-dispatch. The drive then resumed into a
deleted directory.

Real git, real worktrees, under a path with a space in it (the member's workspace lives
under `~/Library/Application Support/…`), and the real `reap_feature_worktree` — nothing
about the reap is stubbed, so a tree this test says is kept is really still on disk.
"""

from __future__ import annotations

import subprocess

from project_board import worktree
from project_board.loop import BoardLoop

ROOT = ".worktrees"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "Application Support" / "workspaces" / "designSystem 9062" / "projects" / "protoContent"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "board-test@localhost", cwd=repo)
    _git("config", "user.name", "Board Test", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    (repo / "README.md").write_text("seed\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _tree(repo, fid, title=""):
    """A real worktree named exactly as the board names it (`worktree_dir` / `branch_name`)."""
    rel = f"{ROOT}/{worktree.worktree_dir(fid, title)}"
    _git("worktree", "add", "-q", "-b", worktree.branch_name(fid, title), rel, "main", cwd=repo)
    return repo / rel


class _Store:
    """Just the reads the sweep makes. Every lane list is empty, so only the worktree reap
    (the half under test) has anything to do."""

    def __init__(self, states):
        self.states = states
        self.asked = []

    def get_feature(self, fid):
        self.asked.append(fid)
        st = self.states.get(fid)
        return {"id": fid, "board_state": st} if st else None

    def list_features(self, state=None, **_kw):
        return []

    def archive_stale(self, archive_after_days=7):
        return []

    def live_cards(self):
        return []


async def test_a_ds_board_keeps_every_live_tree_and_still_reaps_its_dead_ones(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    trees = {
        # pre-PR gate phase: its drive just verified the build and is about to run the gate
        "gate": _tree(repo, "ds-vvi", "plugin hardening follow-ups from vera"),
        # solve phase: coder.solve's candidate trees (never slugged)
        "solve.g1": _tree(repo, "ds-07d.g1"),
        "solve.g2": _tree(repo, "ds-07d.g2"),
        # merge-gate phase: in review, its merged-state gate / auto-merge running on the card
        "merge": _tree(repo, "ds-4df", "archetype match current designsystem agent"),
        # a coder stalled mid-dispatch (the ds-wkt incident): still a live drive
        "stalled": _tree(repo, "ds-wkt", "ui migrate nav app shell badges to count"),
        # the dead ones: a merged card, a cancelled card's candidate, a card that is gone
        "done": _tree(repo, "ds-old", "shipped last week"),
        "cancelled": _tree(repo, "ds-cxl.g1"),
        "gone": _tree(repo, "ds-zzz"),
        # an id whose PREFIX has a hyphen cannot be split from its slug by name alone
        "unparseable": _tree(repo, "my-proj-abc", "some work"),
    }
    store = _Store(
        {
            "ds-vvi": "in_progress",
            "ds-07d": "in_progress",
            "ds-4df": "in_review",
            "ds-wkt": "in_progress",
            "ds-old": "done",
            "ds-cxl": "cancelled",
            "my-proj-abc": "in_progress",
        }
    )
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)
    loop = BoardLoop({"repo": str(repo), "worktrees_root": ROOT})
    # The live-drive registry: the drives own these cards right now.
    loop._inflight_files = {"ds-vvi": set(), "ds-07d": set(), "ds-wkt": set()}
    loop._review_inflight = {"ds-4df"}

    await loop._sweep()

    kept = {k for k, p in trees.items() if p.is_dir()}
    assert kept == {"gate", "solve.g1", "solve.g2", "merge", "stalled", "unparseable"}, kept
    # The store was asked about CARD ids, never a slugged directory remainder.
    assert not any("-plugin" in a or "-archetype" in a for a in store.asked), store.asked
    # Git agrees: the kept trees are still registered worktrees; the reaped ones are gone.
    listed = _git("worktree", "list", "--porcelain", cwd=repo)
    for name in ("gate", "merge", "stalled", "unparseable"):
        assert str(trees[name]) in listed
    for name in ("done", "cancelled", "gone"):
        assert str(trees[name]) not in listed

    # A second sweep changes nothing: the ds board is stable, not reaped on the next pass.
    await loop._sweep()
    assert {k for k, p in trees.items() if p.is_dir()} == kept


async def test_without_a_live_drive_a_ds_tree_follows_its_card_state(monkeypatch, tmp_path):
    """The prefix fix alone, with NO live-drive registry entry: a `ds-` card's slugged tree is
    kept while the card is open and reaped once it is done. Before #461 both were reaped."""
    repo = _repo(tmp_path)
    open_tree = _tree(repo, "ds-a1", "open card")
    done_tree = _tree(repo, "ds-b2", "closed card")
    store = _Store({"ds-a1": "in_review", "ds-b2": "done"})
    monkeypatch.setattr("project_board.loop.get_store", lambda **_kw: store)

    await BoardLoop({"repo": str(repo), "worktrees_root": ROOT})._sweep()

    assert open_tree.is_dir()
    assert not done_tree.exists()
