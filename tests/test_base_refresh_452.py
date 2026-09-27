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


async def test_an_ignored_local_file_upstream_starts_tracking_is_never_overwritten(origin):
    """Review M1: an operator's IGNORED secret.env, and upstream then commits a template at
    that path. A plain ff-only merge replaced the secret; the refresh must refuse instead."""
    Path(origin.seed, ".gitignore").write_text("secret.env\n")
    _git("-C", origin.seed, "add", "-A")
    _git("-C", origin.seed, "commit", "-m", "ignore secret.env")
    _git("-C", origin.seed, "push", "origin", "main")
    await worktree.refresh_base_checkout(origin.clone, "main")  # the ignore rule lands (a clean ff)
    Path(origin.clone, "secret.env").write_text("OPERATOR LOCAL SECRET\n")
    Path(origin.seed, "secret.env").write_text("TEMPLATE\n")
    _git("-C", origin.seed, "add", "-f", "secret.env")
    _git("-C", origin.seed, "commit", "-m", "track template")
    _git("-C", origin.seed, "push", "origin", "main")
    before = origin.head()

    result = await worktree.refresh_base_checkout(origin.clone, "main")
    assert result["state"] == "stale" and "fast-forward refused" in result["detail"]
    assert Path(origin.clone, "secret.env").read_text() == "OPERATOR LOCAL SECRET\n"
    assert origin.head() == before


def _loop(projects: dict, **extra) -> BoardLoop:
    return BoardLoop({"coder": "proto", "merge_poll": False, "projects": projects, **extra})


async def test_only_board_owned_checkouts_are_moved_by_default(origin, tmp_path):
    """Review M2: the operator's own checkout (outside the onboarding root, not registered by
    the board) is never moved unless its project opts in with base_refresh: true."""
    before = origin.head()
    origin.release()
    loop = _loop({"mine": {"repo": origin.clone}})
    await loop._refresh_base_checkouts()
    snap = health.base_checkouts_snapshot()
    assert snap["mine"]["state"] == "skipped" and "base_refresh: true" in snap["mine"]["detail"]
    assert origin.head() == before


async def test_a_checkout_under_the_onboarding_root_is_board_owned(origin, tmp_path, monkeypatch):
    import sys
    import types

    new = origin.release()
    sdk = types.ModuleType("graph.sdk")
    sdk.config = lambda: types.SimpleNamespace(onboarding_enabled=True, onboarding_root=str(tmp_path), plugin_config={})
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)
    await _loop({"web": {"repo": origin.clone}})._refresh_base_checkouts()
    assert health.base_checkouts_snapshot()["web"]["state"] == "fast_forwarded"
    assert origin.head() == new


async def test_the_sweep_refreshes_opted_in_projects_and_publishes_stale_ones(origin, tmp_path):
    """One clean checkout is moved, one dirty one is reported stale, one opted out is left."""
    other = _Origin(tmp_path / "other")
    third = _Origin(tmp_path / "third")
    new = origin.release()
    other.release()
    third_before = third.head()
    third.release()
    Path(other.clone, "README.md").write_text("wip\n")
    loop = _loop(
        {
            "web": {"repo": origin.clone, "base_refresh": True},
            "docs": {"repo": other.clone, "base_refresh": True},
            "ops": {"repo": third.clone, "base_refresh": False, "managed_project": "ops"},
        },
        default_project="web",
    )
    await loop._refresh_base_checkouts()
    snap = health.base_checkouts_snapshot()
    assert snap["web"]["state"] == "fast_forwarded" and origin.head() == new
    assert snap["docs"]["state"] == "stale" and "uncommitted changes" in snap["docs"]["detail"]
    assert snap["docs"]["repo"] == other.clone and snap["docs"]["base"] == "main"
    assert snap["ops"]["state"] == "skipped" and third.head() == third_before


async def test_the_fetches_run_concurrently_under_one_budget(origin, tmp_path, monkeypatch):
    """Review M2: a pass never holds the loop for N × the git timeout. Checkouts refresh
    concurrently, and any still running at the budget are cancelled and reported."""
    import asyncio
    import time

    other = _Origin(tmp_path / "other")
    started = []

    async def slow(repo, base):
        started.append(time.monotonic())
        await asyncio.sleep(30)

    monkeypatch.setattr(worktree, "refresh_base_checkout", slow)
    loop = _loop({"a": {"repo": origin.clone, "base_refresh": True}, "b": {"repo": other.clone, "base_refresh": True}})
    loop.base_refresh_budget_s = 0.3
    t0 = time.monotonic()
    await loop._refresh_base_checkouts()
    assert time.monotonic() - t0 < 2
    assert len(started) == 2 and abs(started[0] - started[1]) < 0.2  # both began together
    snap = health.base_checkouts_snapshot()
    assert snap["a"]["state"] == snap["b"]["state"] == "unknown" and "refresh budget" in snap["a"]["detail"]


async def test_a_running_gate_smoke_holds_the_checkout(origin, monkeypatch):
    """The registration gate smoke and the refresh share the per-checkout lock: a pass
    skips a checkout whose smoke is running, and holds the lock while it refreshes."""
    import asyncio

    from project_board import project_registry

    before = origin.head()
    origin.release()
    lock = project_registry._SMOKE_LOCKS.setdefault(str(Path(origin.clone).resolve()), asyncio.Lock())
    async with lock:
        await _loop({"web": {"repo": origin.clone, "base_refresh": True}})._refresh_base_checkouts()
    assert "gate smoke is running" in health.base_checkouts_snapshot()["web"]["detail"]
    assert origin.head() == before

    held = []
    real = worktree.refresh_base_checkout

    async def spy(repo, base):
        held.append(lock.locked())
        return await real(repo, base)

    monkeypatch.setattr(worktree, "refresh_base_checkout", spy)
    await _loop({"web": {"repo": origin.clone, "base_refresh": True}})._refresh_base_checkouts()
    assert held == [True] and not lock.locked()


async def test_the_sweep_starts_the_refresh_off_the_tick(origin, monkeypatch):
    import asyncio

    gate = asyncio.Event()

    async def blocked(repo, base):
        await gate.wait()
        return {"state": "current", "behind": 0, "detail": ""}

    monkeypatch.setattr(worktree, "refresh_base_checkout", blocked)
    loop = _loop({"web": {"repo": origin.clone, "base_refresh": True}})
    loop._start_base_refresh()
    first = loop._base_refresh_task
    loop._start_base_refresh()  # one pass at a time
    assert loop._base_refresh_task is first and not first.done()
    gate.set()
    await first
    assert health.base_checkouts_snapshot()["web"]["state"] == "current"


async def test_base_refresh_false_leaves_every_checkout_alone(origin):
    before = origin.head()
    origin.release()
    loop = _loop({"web": {"repo": origin.clone, "base_refresh": True}}, base_refresh=False)
    health.publish_base_checkouts({})
    await loop._refresh_base_checkouts()
    loop._start_base_refresh()
    assert getattr(loop, "_base_refresh_task", None) is None
    assert origin.head() == before
    assert health.base_checkouts_snapshot() == {}
