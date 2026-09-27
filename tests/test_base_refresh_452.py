"""Base-checkout refresh (#452, the staleness comment) — against REAL git.

Worktrees are cut from ``origin/<base>``, but the board's MAIN checkout (the tree the agent
reads, and the one registered as a managed project) was never moved: an agent read a v0.7.1
tree an hour after v0.9.0 shipped. ``worktree.refresh_base_checkout`` fetches the base and
fast-forwards the checkout ONLY when it is clean and on the base branch; anything else is left
untouched and reported ``stale``.

Everything here shells real git against a temporary bare origin + clone — the whole point of
the seam is what git does to a working tree, which no fake can show.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import REAL_SEAMS

from project_board import health, worktree
from project_board.loop import BoardLoop


@pytest.fixture(autouse=True)
def real_refresh(monkeypatch):
    """conftest stubs the refresh for the unit tier; this tier runs the genuine seam."""
    monkeypatch.setattr(worktree, "refresh_base_checkout", REAL_SEAMS["worktree.refresh_base_checkout"])


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

    def release(self, text: str = "v0.9.0\n", name: str = "README.md") -> str:
        """Push a new commit to origin's base (a release lands) and return its sha."""
        Path(self.seed, name).write_text(text)
        _git("-C", self.seed, "add", "-A")
        _git("-C", self.seed, "commit", "-m", text.strip())
        _git("-C", self.seed, "push", "origin", self.base)
        return _git("-C", self.seed, "rev-parse", "HEAD")

    def head(self) -> str:
        return _git("-C", self.clone, "rev-parse", "HEAD")


@pytest.fixture
def origin(tmp_path):
    return _Origin(tmp_path)


async def test_a_clean_checkout_on_base_is_fast_forwarded(origin):
    new = origin.release()
    result = await worktree.refresh_base_checkout(origin.clone, "main")
    assert result == {"state": "fast_forwarded", "behind": 1, "detail": ""}
    assert origin.head() == new
    assert Path(origin.clone, "README.md").read_text() == "v0.9.0\n"  # the tree the agent reads moved


async def test_an_up_to_date_checkout_is_current(origin):
    before = origin.head()
    assert await worktree.refresh_base_checkout(origin.clone, "main") == {"state": "current", "behind": 0, "detail": ""}
    assert origin.head() == before


async def test_uncommitted_edits_are_never_touched_and_read_stale(origin):
    before = origin.head()
    origin.release()
    Path(origin.clone, "README.md").write_text("operator's local edit\n")
    result = await worktree.refresh_base_checkout(origin.clone, "main")
    assert result["state"] == "stale" and result["behind"] == 1
    assert "uncommitted changes to README.md" in result["detail"]
    assert origin.head() == before
    assert Path(origin.clone, "README.md").read_text() == "operator's local edit\n"


async def test_a_checkout_on_another_branch_is_not_switched(origin):
    origin.release()
    _git("-C", origin.clone, "checkout", "-q", "-b", "operator-work")
    result = await worktree.refresh_base_checkout(origin.clone, "main")
    assert result["state"] == "stale"
    assert "'operator-work'" in result["detail"]
    assert _git("-C", origin.clone, "rev-parse", "--abbrev-ref", "HEAD") == "operator-work"


async def test_local_commits_the_remote_lacks_are_not_rewritten(origin):
    origin.release()
    Path(origin.clone, "local.txt").write_text("mine\n")
    _git("-C", origin.clone, "add", "-A")
    _git("-C", origin.clone, "commit", "-m", "local only")
    mine = origin.head()
    result = await worktree.refresh_base_checkout(origin.clone, "main")
    assert result["state"] == "stale" and "diverged" in result["detail"]
    assert origin.head() == mine


async def test_an_unreachable_origin_is_unknown_not_an_error(origin, tmp_path):
    _git("-C", origin.clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    result = await worktree.refresh_base_checkout(origin.clone, "main")
    assert result["state"] == "unknown" and "git fetch origin main failed" in result["detail"]


async def test_the_sweep_refreshes_every_project_and_publishes_stale_ones(origin, tmp_path):
    """The loop's sweep step: one clean checkout is moved, one dirty one is reported stale,
    and both land in the health snapshot /status serves."""
    other = _Origin(tmp_path / "other")
    new = origin.release()
    other.release()
    Path(other.clone, "README.md").write_text("wip\n")
    loop = BoardLoop(
        {
            "coder": "proto",
            "merge_poll": False,
            "projects": {"web": {"repo": origin.clone}, "docs": {"repo": other.clone}},
            "default_project": "web",
        }
    )
    await loop._refresh_base_checkouts()
    snap = health.base_checkouts_snapshot()
    assert snap["web"]["state"] == "fast_forwarded" and origin.head() == new
    assert snap["docs"]["state"] == "stale" and "uncommitted changes" in snap["docs"]["detail"]
    assert snap["docs"]["repo"] == other.clone and snap["docs"]["base"] == "main"


async def test_base_refresh_false_leaves_every_checkout_alone(origin):
    before = origin.head()
    origin.release()
    loop = BoardLoop({"coder": "proto", "merge_poll": False, "repo": origin.clone, "base_refresh": False})
    health.publish_base_checkouts({})
    await loop._refresh_base_checkouts()
    assert origin.head() == before
    assert health.base_checkouts_snapshot() == {}
