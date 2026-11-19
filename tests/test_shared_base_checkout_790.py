"""Shared checkout conflicts and stale origin bases must never trigger repair."""

from unittest.mock import AsyncMock
from pathlib import Path

import pytest
from conftest import REAL_SEAMS

from project_board import health, worktree
from project_board.projects import resolve_projects
from test_preflight_off_base_checkout import _Origin, _branch, _capture_pages, _git, _loop


def _projects(repo, bases):
    return {
        name: {"repo": str(repo), "base_branch": base, "base_refresh": True}
        for name, base in zip(("app", "epic-a", "epic-b"), bases)
    }


async def test_origin_branch_exists_reads_exact_live_head(tmp_path):
    origin = _Origin(tmp_path)
    assert await worktree.origin_branch_exists(origin.clone, "main") is True
    origin.park("epic/a", push=True)
    assert await worktree.origin_branch_exists(origin.clone, "epic/a") is True
    # Neither a suffix match nor a tag is evidence of the requested branch.
    assert await worktree.origin_branch_exists(origin.clone, "a") is False
    _git("-C", origin.seed, "tag", "tag-only")
    _git("-C", origin.seed, "push", "origin", "refs/tags/tag-only")
    assert await worktree.origin_branch_exists(origin.clone, "tag-only") is False
    _git("-C", origin.seed, "push", "origin", "--delete", "epic/a")
    assert _git("-C", origin.clone, "rev-parse", "origin/epic/a")
    assert await worktree.origin_branch_exists(origin.clone, "epic/a") is False


async def test_origin_branch_exists_unavailable_origin_is_uncertain(tmp_path):
    origin = _Origin(tmp_path)
    _git("-C", origin.clone, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    with pytest.raises(worktree.WorktreeError, match="git ls-remote failed"):
        await worktree.origin_branch_exists(origin.clone, "main")


@pytest.mark.parametrize("condition", ["clean", "dirty", "unowned", "refresh_failure"])
async def test_conflicting_checkout_keeps_refreshing_current_base(tmp_path, monkeypatch, condition):
    origin = _Origin(tmp_path)
    before = origin.head()
    projects = _projects(origin.clone, ["main", "epic/a", "epic/b"])
    if condition == "unowned":
        for settings in projects.values():
            settings["base_refresh"] = False
    if condition == "dirty":
        Path(origin.clone, "README.md").write_text("local edits\n")
    lp = _loop(projects)
    pages = _capture_pages(lp)
    switch = AsyncMock()
    monkeypatch.setattr(worktree, "switch_base_checkout", switch)
    refresh = AsyncMock(wraps=REAL_SEAMS["worktree.refresh_base_checkout"])
    if condition == "refresh_failure":
        refresh.side_effect = OSError("remote unavailable")
    monkeypatch.setattr(worktree, "refresh_base_checkout", refresh)

    for index in range(3):
        latest = origin.release(f"release {index}\n")
        await lp._refresh_base_checkouts()
        assert origin.head() == (latest if condition == "clean" else before)
        assert _branch(origin.clone) == "main"
        rows = health.base_checkouts_snapshot()
        assert set(rows) == set(projects)
        for row in rows.values():
            assert row["state"] == "conflict"
            for label in ("app (main)", "epic-a (epic/a)", "epic-b (epic/b)"):
                assert label in row["detail"]

    assert refresh.await_count == (0 if condition == "unowned" else 3)
    for call in refresh.await_args_list:
        assert call.args == (origin.clone, "main")
    switch.assert_not_awaited()
    assert pages == []
    assert lp._off_base_passes == {}
    if condition == "dirty":
        assert Path(origin.clone, "README.md").read_text() == "local edits\n"


async def test_conflicting_projects_never_switch(tmp_path, monkeypatch, caplog):
    origin = _Origin(tmp_path)
    origin.park("release", push=True)
    projects = _projects(origin.clone, ["main", "epic/a", "epic/b"])
    alias = tmp_path / "alias"
    alias.symlink_to(origin.clone, target_is_directory=True)
    projects["epic-b"]["repo"] = str(alias)
    lp = _loop(projects)
    pages = _capture_pages(lp)
    switch = AsyncMock()
    monkeypatch.setattr(worktree, "switch_base_checkout", switch)
    caplog.clear()  # config-load warnings are independent of sweep warnings
    for _ in range(4):
        await lp._refresh_base_checkouts()
        rows = health.base_checkouts_snapshot()
        assert set(rows) == set(projects)
        for row in rows.values():
            assert row["state"] == "conflict"
            for label in ("app (main)", "epic-a (epic/a)", "epic-b (epic/b)"):
                assert label in row["detail"]
    switch.assert_not_awaited()
    assert pages == []
    assert _branch(origin.clone) == "release"
    assert len([r for r in caplog.records if "config conflict" in r.message]) == 1

    # Removing the conflict clears the condition and starts the normal second-pass rule.
    lp._projects = {"app": lp._projects["app"]}
    await lp._refresh_base_checkouts()
    switch.assert_not_awaited()
    assert health.base_checkouts_snapshot()["app"]["state"] == "stale"


async def test_deleted_origin_base_is_stale_even_with_local_tracking_ref(tmp_path, monkeypatch, caplog):
    origin = _Origin(tmp_path)
    origin.park("release", push=True)
    _git("-C", origin.origin, "symbolic-ref", "HEAD", "refs/heads/release")
    _git("-C", origin.seed, "push", "origin", "--delete", "main")
    assert _git("-C", origin.clone, "rev-parse", "origin/main")  # stale local ref survives
    lp = _loop(_projects(origin.clone, ["main"]))
    pages = _capture_pages(lp)
    switch = AsyncMock()
    monkeypatch.setattr(worktree, "switch_base_checkout", switch)
    for _ in range(4):
        await lp._refresh_base_checkouts()
    switch.assert_not_awaited()
    assert pages == []
    row = health.base_checkouts_snapshot()["app"]
    assert row["state"] == "stale"
    assert "base branch main no longer exists on origin" in row["detail"]
    assert len([r for r in caplog.records if "no longer exists on origin" in r.message]) == 1

    # Recreating the remote branch clears the stale evidence and permits recovery.
    _git("-C", origin.seed, "push", "origin", "main")
    switch.return_value = {"state": "switched"}
    await lp._refresh_base_checkouts()
    switch.assert_awaited_once_with(origin.clone, "main")


@pytest.mark.parametrize("failure", ["returncode", "exception"])
async def test_remote_failure_never_switches_or_pages(tmp_path, monkeypatch, failure):
    origin = _Origin(tmp_path)
    origin.park("release", push=True)
    lp = _loop(_projects(origin.clone, ["main"]))
    pages = _capture_pages(lp)
    switch = AsyncMock()
    monkeypatch.setattr(worktree, "switch_base_checkout", switch)
    real_git = worktree._git

    async def failing_git(repo, *args, **kwargs):
        if args[0] == "ls-remote":
            if failure == "exception":
                raise OSError("network unavailable")
            return 128, "", "network unavailable"
        return await real_git(repo, *args, **kwargs)

    monkeypatch.setattr(worktree, "_git", failing_git)
    for _ in range(3):
        await lp._refresh_base_checkouts()
    switch.assert_not_awaited()
    assert pages == []
    assert _branch(origin.clone) == "release"
    monkeypatch.setattr(worktree, "_git", real_git)
    switch.return_value = {"state": "switched"}
    await lp._refresh_base_checkouts()
    switch.assert_awaited_once()


@pytest.mark.parametrize("bases, count", [(["main", "epic/a"], 1), (["main", "main"], 0)])
def test_config_warns_once_for_different_bases(tmp_path, caplog, bases, count):
    alias = tmp_path / "alias"
    repo = tmp_path / "repo"
    repo.mkdir()
    alias.symlink_to(repo, target_is_directory=True)
    projects = _projects(repo, bases)
    projects["epic-a"]["repo"] = str(alias)
    resolved = resolve_projects({"projects": projects})
    assert set(resolved) == set(projects)
    warnings = [r for r in caplog.records if "config conflict" in r.message]
    assert len(warnings) == count
    if count:
        assert "app (main)" in warnings[0].message
        assert "epic-a (epic/a)" in warnings[0].message


async def test_shared_checkout_same_base_still_recovers(tmp_path):
    origin = _Origin(tmp_path)
    origin.park("release", push=True)
    lp = _loop(_projects(origin.clone, ["main", "main"]))
    pages = _capture_pages(lp)
    for _ in range(2):
        await lp._refresh_base_checkouts()
    assert _branch(origin.clone) == "main"
    assert pages == []
    assert all(row["state"] != "conflict" for row in health.base_checkouts_snapshot().values())
