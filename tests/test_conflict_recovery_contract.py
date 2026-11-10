"""Real Git histories at the board's edit-only recovery boundary."""

import json
import subprocess

import pytest

from project_board import worktree
from project_board.loop.prompt import integration_prompt


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def history(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    repo = tmp_path / "repo"
    git(tmp_path, "clone", str(remote), str(repo))
    git(repo, "config", "user.email", "board@example.test")
    git(repo, "config", "user.name", "Board")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "checkout", "-b", "main")
    for name in ("a", "b"):
        (repo / name).write_text("initial\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial clean checkout")
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "-b", "feature")
    for name in ("a", "b"):
        (repo / name).write_text("feature coverage\n")
    git(repo, "commit", "-am", "feature tests")
    git(repo, "push", "origin", "feature")
    return repo


def sibling(repo, name):
    git(repo, "checkout", "-b", f"sibling-{name}", "main")
    (repo / name).write_text(f"sibling {name} coverage\n")
    git(repo, "commit", "-am", f"sibling {name}")
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "-m", f"Merge sibling {name}", f"sibling-{name}")
    git(repo, "push", "origin", "main")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "feature")
    return sha


async def test_siblings_restart_and_edit_only_resolution(history):
    repo = history
    first = sibling(repo, "a")
    context = await worktree.prepare_integration(str(repo), "feature", "main")
    assert context["base_sha"] == first
    assert context["conflict_paths"] == ["a"]
    # The coder can write files while all Git metadata is read-only.
    metadata = repo / ".git"
    modes = [(p, p.stat().st_mode) for p in [metadata, *metadata.rglob("*")]]
    try:
        for p, mode in modes:
            p.chmod(mode & ~0o222)
        (repo / "a").write_text("feature coverage\nsibling a coverage\n")
    finally:
        for p, mode in modes:
            p.chmod(mode)
    await worktree.finish_integration(str(repo), "feature", "main")
    assert git(repo, "merge-base", "--is-ancestor", first, "HEAD") == ""
    git(repo, "push", "origin", "feature")
    second = sibling(repo, "b")
    # The reconcile probe records fresh evidence even before its budget decision.
    result, paths = await worktree.rebase_onto_base(str(repo), "feature", "main")
    assert result == "conflict"
    assert paths == "b"
    # No in-memory loop state is needed after restart / automatic unblock.
    persisted = await worktree.recovery_context(str(repo), "feature")
    assert persisted["base_sha"] == second
    assert persisted["conflict_paths"] == ["b"]
    prompt = integration_prompt(persisted)
    assert "Actual conflict paths: b." in prompt
    context = await worktree.prepare_integration(str(repo), "feature", "main")
    assert context["conflict_paths"] == ["b"]
    with pytest.raises(worktree.IntegrationRequired):
        await worktree.finish_integration(str(repo), "feature", "main")
    (repo / "b").write_text("feature coverage\nsibling b coverage\n")
    await worktree.finish_integration(str(repo), "feature", "main")
    assert git(repo, "merge-base", "--is-ancestor", second, "HEAD") == ""
    assert not git(repo, "ls-files", "--unmerged")
    for name in ("a", "b"):
        assert (repo / name).read_text() == f"feature coverage\nsibling {name} coverage\n"
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-commit", "--no-ff", "feature")
    assert not git(repo, "ls-files", "--unmerged")


async def test_base_movement_during_resolution(history, tmp_path):
    repo = history
    sibling(repo, "a")
    await worktree.prepare_integration(str(repo), "feature", "main")
    other = tmp_path / "sibling"
    git(repo, "worktree", "add", str(other), "main")
    (other / "b").write_text("sibling b coverage\n")
    git(other, "commit", "-am", "second sibling")
    git(other, "push", "origin", "main")
    latest = git(other, "rev-parse", "HEAD")
    (repo / "a").write_text("feature coverage\nsibling a coverage\n")
    with pytest.raises(worktree.IntegrationRequired, match="advanced"):
        await worktree.finish_integration(str(repo), "feature", "main")
    context = await worktree.recovery_context(str(repo), "feature")
    assert context["base_sha"] == latest
    assert context["conflict_paths"] == ["b"]
    (repo / "b").write_text("feature coverage\nsibling b coverage\n")
    await worktree.finish_integration(str(repo), "feature", "main")
    assert git(repo, "merge-base", "--is-ancestor", latest, "HEAD") == ""


async def test_content_only_commit_is_not_integration(history):
    repo = history
    base = sibling(repo, "a")
    head = git(repo, "rev-parse", "HEAD")
    await worktree._record_recovery(
        str(repo),
        "feature",
        {"kind": "merge", "base": "main", "base_sha": base, "head_sha": head, "conflict_paths": ["a"]},
    )
    (repo / "a").write_text("feature coverage\nsibling a coverage\n")
    with pytest.raises(worktree.IntegrationRequired, match="pinned base"):
        await worktree.finish_integration(str(repo), "feature", "main")
    assert git(repo, "rev-parse", "HEAD^") == head


async def test_failed_fetch_and_interrupted_rebase_are_preserved(history):
    repo = history
    sibling(repo, "a")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "remote", "set-url", "origin", str(repo / "missing"))
    with pytest.raises(worktree.WorktreeError, match="fetch failed"):
        await worktree.prepare_integration(str(repo), "feature", "main")
    assert git(repo, "rev-parse", "HEAD") == head
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    rebase = subprocess.run(["git", "-C", str(repo), "rebase", "main"], capture_output=True)
    assert rebase.returncode != 0
    state = repo / ".git" / "rebase-merge"
    assert state.is_dir()
    with pytest.raises(worktree.IntegrationRequired, match="Interrupted rebase"):
        await worktree.commit_worktree(str(repo), "must not commit")
    assert state.exists()


async def test_clean_index_pending_merge_is_committed(history):
    repo = history
    sibling(repo, "a")
    await worktree.prepare_integration(str(repo), "feature", "main")
    # Choosing the feature's entire file still requires a two-parent merge commit.
    (repo / "a").write_text("feature coverage\n")
    git(repo, "add", "a")
    assert not git(repo, "status", "--porcelain")
    await worktree.commit_worktree(str(repo), "resolve")
    assert len(git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    context = await worktree.recovery_context(str(repo), "feature")
    assert json.dumps(context)


async def test_exhausted_budget_auto_unblock_refreshes_then_restart_recovers(history, monkeypatch):
    from unittest.mock import AsyncMock

    from project_board.loop import BoardLoop
    from tests.test_loop import _BlockedStore, _CiStore, _blocked

    repo = history
    fid, title = "bd-recovery", "Recover coverage"
    branch = worktree.branch_name(fid, title)
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    # Sibling A conflicts while this feature remains on its initial clean checkout.
    git(repo, "checkout", "main")
    (repo / "a").write_text("sibling a coverage\n")
    git(repo, "commit", "-am", "A")
    git(repo, "push", "origin", "main")
    feature = {"id": fid, "title": title, "pr_url": "https://example/pr/1", "labels": ["budget:rebase:1"]}
    loop = BoardLoop({"coder": "proto", "rebase_fix_max": 1})
    monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
    monkeypatch.setattr(worktree, "pr_merge_state", AsyncMock(return_value="DIRTY"))
    store = _CiStore(feature)
    assert await loop._maybe_rebase(store, feature, feature["pr_url"], str(repo))
    assert store.blocked and not store.requeued
    assert "a" in (await worktree.recovery_context(str(repo), branch))["conflict_paths"]
    # Repair A with the actual create/dispatch/commit contract.
    path, actual = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    from pathlib import Path

    (Path(path) / "a").write_text("feature coverage\nsibling a coverage\n")
    await worktree.finish_integration(path, actual, "main")
    unpublished = git(path, "rev-parse", "HEAD")
    assert not (Path(path) / ".git").is_dir()  # linked-worktree Git metadata
    assert await worktree.pending_integration(str(repo), ".worktrees", fid, title) == (path, actual)
    resumed, _ = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    assert resumed == path and git(path, "rev-parse", "HEAD") == unpublished
    git(path, "push", "origin", branch)
    git(repo, "worktree", "remove", path)
    # Sibling B arrives before automatic unblock: old A feedback MUST disappear.
    (repo / "b").write_text("sibling b coverage\n")
    git(repo, "commit", "-am", "B")
    git(repo, "push", "origin", "main")
    row = {**_blocked(fid, "merge-conflict", title=title), "pr_url": feature["pr_url"], "labels": ["budget:rebase:1"]}
    blocked = _BlockedStore([row])
    restarted = BoardLoop({"coder": "proto", "rebase_fix_max": 1})
    monkeypatch.setattr(restarted, "_repo_for", lambda _: str(repo))
    monkeypatch.setattr(restarted, "_base_branch_for", lambda _: "main")
    await restarted._recover_blocked(blocked)
    assert blocked.cleared == [fid] and blocked.requeued == [fid]
    assert "Actual conflict paths: b." in restarted._ci_feedback[fid]
    path, actual = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    assert (await worktree.recovery_context(path, actual))["conflict_paths"] == ["b"]
    (Path(path) / "b").write_text("feature coverage\nsibling b coverage\n")
    # Simulate another restart after file edits, before board staging/commit.
    assert await worktree.pending_integration(str(repo), ".worktrees", fid, title) == (path, actual)
    same, _ = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    assert same == path
    assert "sibling b coverage" in (Path(path) / "b").read_text()
    await worktree.finish_integration(path, actual, "main")
    git(path, "merge-base", "--is-ancestor", "origin/main", "HEAD")
    for name in ("a", "b"):
        assert (Path(path) / name).read_text() == f"feature coverage\nsibling {name} coverage\n"


@pytest.mark.parametrize("resolve", [False, True])
async def test_drive_checks_integration_before_review(history, monkeypatch, resolve):
    from pathlib import Path
    from unittest.mock import AsyncMock

    from tests.test_loop import FEATURE, _drive_with

    repo = history
    sibling(repo, "b")
    feature = {**FEATURE, "pr_url": "https://example/pr/7"}
    branch = worktree.branch_name(feature["id"], feature["title"])
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    git(repo, "checkout", "main")
    create = worktree.create_worktree
    opened = AsyncMock(return_value=feature["pr_url"])
    gate = AsyncMock(return_value=None)
    prompts = []

    def seed(loop):
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(worktree, "create_worktree", create)
        loop._ci_feedback[feature["id"]] = "Your branch now CONFLICTS with main: stale-a.py"

    async def dispatch(coder, path, prompt, **kwargs):
        prompts.append(prompt)
        assert "Actual conflict paths: b." in prompt
        assert "stale-a.py" not in prompt
        # An unrelated content edit is not evidence that the merge was resolved.
        (Path(path) / "extra-test").write_text("feature extra test\n")
        if resolve:
            (Path(path) / "b").write_text("feature coverage\nsibling b coverage\n")
        return "## Summary\nResolved coverage"

    loop, store = await _drive_with(
        monkeypatch,
        feature=feature,
        seed=seed,
        dispatch=dispatch,
        open_pr=opened,
        gate=gate,
        cfg={"coder": "proto", "local_gate_cmd": "check", "max_mode_n": 3},
    )
    assert len(prompts) == 1
    if resolve:
        opened.assert_awaited_once()
        path = opened.call_args.args[0]
        git(path, "merge-base", "--is-ancestor", "origin/main", "HEAD")
        assert (Path(path) / "b").read_text() == "feature coverage\nsibling b coverage\n"
        assert "open_review" in store.names()
    else:
        opened.assert_not_awaited()
        gate.assert_not_awaited()
        assert "flag_blocked" in store.names()
        assert not loop._ci_fix_attempts and not loop._review_fix_attempts
        pending = await worktree.pending_integration(str(repo), loop.root, feature["id"], feature["title"])
        assert pending
        assert (Path(pending[0]) / "extra-test").read_text() == "feature extra test\n"


async def test_failed_publication_fetch_never_pushes(history, monkeypatch):
    from unittest.mock import AsyncMock

    repo = history
    sibling(repo, "a")
    await worktree.prepare_integration(str(repo), "feature", "main")
    (repo / "a").write_text("feature coverage\nsibling a coverage\n")
    git(repo, "remote", "set-url", "origin", str(repo / "absent"))
    gh = AsyncMock(side_effect=AssertionError("must not publish"))
    monkeypatch.setattr(worktree, "_gh", gh)
    with pytest.raises(worktree.WorktreeError, match="fetch failed"):
        await worktree.open_pr(str(repo), "feature", base="main", title="repair")
    gh.assert_not_awaited()
    assert (repo / "a").read_text() == "feature coverage\nsibling a coverage\n"


@pytest.mark.parametrize("staged", [False, True])
async def test_markers_cannot_finish_interrupted_merge(history, staged):
    repo = history
    sibling(repo, "a")
    await worktree.prepare_integration(str(repo), "feature", "main")
    head = git(repo, "rev-parse", "HEAD")
    with (repo / "a").open("a") as stream:
        stream.write("whitespace error  \n")
    if staged:
        git(repo, "add", "a")
    with pytest.raises(worktree.IntegrationRequired, match="merge content"):
        await worktree.commit_worktree(str(repo), "must refuse markers")
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / ".git" / "MERGE_HEAD").exists()


@pytest.mark.parametrize("kind", ["binary", "binary-attribute", "modify-delete", "delete-modify"])
async def test_untouched_markerless_conflict_preserves_index_and_head(history, kind):
    repo = history
    # Start a separate conflicting path from the shared ancestor.
    git(repo, "checkout", "main")
    if kind == "binary-attribute":
        (repo / ".gitattributes").write_text("opaque binary\n")
        git(repo, "add", ".gitattributes")
    (repo / "opaque").write_bytes(b"initial\0bytes" if kind == "binary" else b"initial\n")
    git(repo, "add", "opaque")
    git(repo, "commit", "-m", "shared file")
    git(repo, "checkout", "feature")
    git(repo, "merge", "main", "-m", "shared base")
    if kind == "delete-modify":
        git(repo, "rm", "opaque")
    else:
        (repo / "opaque").write_bytes(b"feature\0bytes" if kind == "binary" else b"feature\n")
    git(repo, "commit", "-am", "feature change")
    git(repo, "checkout", "main")
    if kind == "modify-delete":
        git(repo, "rm", "opaque")
    else:
        (repo / "opaque").write_bytes(b"sibling\0bytes" if kind == "binary" else b"sibling\n")
    git(repo, "commit", "-am", "sibling change")
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "feature")
    await worktree.prepare_integration(str(repo), "feature", "main")
    head = git(repo, "rev-parse", "HEAD")
    unmerged = git(repo, "ls-files", "--unmerged")
    contents = (repo / "opaque").read_bytes()
    for _ in range(2):  # A retry must not silently stage the same untouched file.
        with pytest.raises(worktree.IntegrationRequired, match="markerless conflict"):
            await worktree.finish_integration(str(repo), "feature", "main")
        assert git(repo, "rev-parse", "HEAD") == head
        assert git(repo, "ls-files", "--unmerged") == unmerged
        assert (repo / "opaque").read_bytes() == contents
    # Edit-only resolution remains possible without changing Git metadata.
    (repo / "opaque").write_bytes(b"feature and sibling\0bytes" if kind == "binary" else b"resolved\n")
    await worktree.finish_integration(str(repo), "feature", "main")
    git(repo, "merge-base", "--is-ancestor", "origin/main", "HEAD")
    assert not git(repo, "ls-files", "--unmerged")


@pytest.mark.parametrize("staged", [False, True])
async def test_resolved_merge_allows_whitespace_in_resolution_and_sibling(history, staged):
    repo = history
    sibling(repo, "a")
    git(repo, "checkout", "main")
    (repo / "sibling-test").write_text("sibling coverage  \n\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "sibling whitespace")
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "feature")
    await worktree.prepare_integration(str(repo), "feature", "main")
    (repo / "a").write_text("feature coverage  \nsibling a coverage\n\n")
    if staged:
        git(repo, "add", "a")
    await worktree.finish_integration(str(repo), "feature", "main")
    git(repo, "merge-base", "--is-ancestor", "origin/main", "HEAD")
    assert (repo / "a").read_text() == "feature coverage  \nsibling a coverage\n\n"
    assert (repo / "sibling-test").read_text() == "sibling coverage  \n\n"
    assert not git(repo, "ls-files", "--unmerged")


@pytest.mark.parametrize("resolution", ["delete", "regular", "symlink"])
async def test_symlink_conflict_requires_and_accepts_explicit_edit(history, resolution):
    repo = history
    git(repo, "checkout", "main")
    link = repo / "link"
    link.symlink_to("initial-target")
    git(repo, "add", "link")
    git(repo, "commit", "-m", "shared symlink")
    git(repo, "checkout", "feature")
    git(repo, "merge", "main", "-m", "shared base")
    link.unlink()
    link.symlink_to("feature-target")
    git(repo, "commit", "-am", "feature target")
    git(repo, "checkout", "main")
    link.unlink()
    link.symlink_to("sibling-target")
    git(repo, "commit", "-am", "sibling target")
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "feature")
    await worktree.prepare_integration(str(repo), "feature", "main")
    head = git(repo, "rev-parse", "HEAD")
    unmerged = git(repo, "ls-files", "--unmerged")
    with pytest.raises(worktree.IntegrationRequired, match="markerless"):
        await worktree.commit_worktree(str(repo), "untouched")
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "ls-files", "--unmerged") == unmerged
    link.unlink()
    if resolution == "regular":
        # Same bytes, different type: still an explicit resolution.
        link.write_text("feature-target")
    elif resolution == "symlink":
        link.symlink_to("resolved-target")
    await worktree.finish_integration(str(repo), "feature", "main")
    assert not git(repo, "ls-files", "--unmerged")
    git(repo, "merge-base", "--is-ancestor", "origin/main", "HEAD")
    if resolution == "regular":
        assert not link.is_symlink() and link.read_text() == "feature-target"
    elif resolution == "symlink":
        assert str(link.readlink()) == "resolved-target"
    else:
        assert not link.is_symlink() and not link.exists()


async def test_published_recovery_resumes_new_remote_commits(history, monkeypatch):
    from pathlib import Path
    from unittest.mock import AsyncMock

    repo = history
    fid, title = "bd-published", "Published recovery"
    branch = worktree.branch_name(fid, title)
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    (Path(path) / "resolution").write_text("unique resolution\n")
    await worktree.finish_integration(path, branch, "main")
    assert await worktree.pending_integration(str(repo), ".worktrees", fid, title)
    monkeypatch.setattr(worktree, "_gh", AsyncMock(return_value=(0, "https://example/pr/1", "")))
    await worktree.open_pr(path, branch, title=title)
    assert await worktree.pending_integration(str(repo), ".worktrees", fid, title) is None
    git(repo, "checkout", "-b", "remote-editor", f"origin/{branch}")
    (repo / "new-test").write_text("new remote coverage\n")
    git(repo, "add", "new-test")
    git(repo, "commit", "-m", "new PR commit")
    latest = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", f"HEAD:{branch}")
    assert await worktree.pending_integration(str(repo), ".worktrees", fid, title) is None
    path, _ = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    assert git(path, "rev-parse", "HEAD") == latest
    assert (Path(path) / "resolution").read_text() == "unique resolution\n"
    assert (Path(path) / "new-test").read_text() == "new remote coverage\n"


@pytest.mark.parametrize("remote_available", [True, False])
async def test_deleted_feature_falls_back_but_failed_fetch_blocks(history, remote_available):
    repo = history
    fid, title = "bd-deleted", "Deleted branch"
    branch = worktree.branch_name(fid, title)
    git(repo, "push", "origin", f"HEAD:{branch}")
    # Delete directly in the bare remote, leaving a stale tracking ref locally.
    remote = git(repo, "remote", "get-url", "origin")
    git(remote, "update-ref", "-d", f"refs/heads/{branch}")
    assert git(repo, "rev-parse", f"origin/{branch}")
    if not remote_available:
        git(repo, "remote", "set-url", "origin", str(repo / "unavailable"))
        with pytest.raises(worktree.IntegrationRequired, match="fetch failed"):
            await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
        return
    path, _ = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    assert git(path, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")
    assert (await worktree.recovery_context(path, branch))["conflict_paths"] == []


async def test_remote_advance_cannot_be_overwritten_by_unpublished_recovery(history):
    repo = history
    await worktree.prepare_integration(str(repo), "feature", "main")
    (repo / "resolution").write_text("unpublished work\n")
    await worktree.finish_integration(str(repo), "feature", "main")
    local = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "remote-editor", "origin/feature")
    (repo / "new-test").write_text("new remote coverage\n")
    git(repo, "add", "new-test")
    git(repo, "commit", "-m", "new PR commit")
    latest = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", "HEAD:feature")
    git(repo, "checkout", "feature")
    with pytest.raises(worktree.IntegrationRequired, match="PR head advanced"):
        await worktree.open_pr(str(repo), "feature", title="recovery")
    assert git(repo, "rev-parse", "HEAD") == local
    assert git(repo, "ls-remote", "origin", "refs/heads/feature").split()[0] == latest
