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


@pytest.mark.parametrize("resolved", [(), ("ours",), ("theirs",), ("ours", "theirs")])
@pytest.mark.parametrize("recreate_source", [False, True])
async def test_rename_rename_requires_destination_edits_only(history, resolved, recreate_source):
    repo = history
    git(repo, "checkout", "main")
    (repo / "original").write_text("shared coverage\n")
    git(repo, "add", "original")
    git(repo, "commit", "-m", "shared rename source")
    git(repo, "checkout", "feature")
    git(repo, "merge", "main", "-m", "shared base")
    git(repo, "mv", "original", "ours")
    git(repo, "commit", "-m", "feature rename")
    git(repo, "checkout", "main")
    git(repo, "mv", "original", "theirs")
    git(repo, "commit", "-m", "sibling rename")
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "feature")
    context = await worktree.prepare_integration(str(repo), "feature", "main")
    head = git(repo, "rev-parse", "HEAD")
    unmerged = git(repo, "ls-files", "--unmerged")
    stages = {}
    for entry in unmerged.splitlines():
        info, name = entry.split("\t")
        stages.setdefault(name, set()).add(info.split()[2])
    assert stages == {"original": {"1"}, "ours": {"2"}, "theirs": {"3"}}
    assert not (repo / "original").exists()
    if recreate_source:
        (repo / "original").write_text("recreated coverage\n")
    for name in resolved:
        (repo / name).write_text(f"shared coverage\nresolved {name} coverage\n")
    if len(resolved) < 2:
        with pytest.raises(worktree.IntegrationRequired, match="explicit resolution: (ours|theirs)"):
            await worktree.commit_worktree(str(repo), "untouched rename destination")
        assert git(repo, "rev-parse", "HEAD") == head
        assert git(repo, "ls-files", "--unmerged") == unmerged
        return
    await worktree.commit_worktree(str(repo), "resolve rename destinations")
    assert not git(repo, "ls-files", "--unmerged")
    assert git(repo, "rev-parse", "HEAD^1") == head
    assert git(repo, "rev-parse", "HEAD^2") == context["integration_sha"]
    assert (repo / "original").exists() == recreate_source
    for name in resolved:
        assert (repo / name).read_text() == f"shared coverage\nresolved {name} coverage\n"


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


@pytest.mark.parametrize("conflicting", [False, True])
async def test_remote_advance_integrates_unpublished_recovery(history, monkeypatch, conflicting):
    repo = history
    await worktree.prepare_integration(str(repo), "feature", "main")
    (repo / "resolution").write_text("unpublished work\n")
    await worktree.finish_integration(str(repo), "feature", "main")
    local = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "remote-editor", "origin/feature")
    (repo / "new-test").write_text("new remote coverage\n")
    if conflicting:
        (repo / "resolution").write_text("remote resolution\n")
        git(repo, "add", "resolution")
    git(repo, "add", "new-test")
    git(repo, "commit", "-m", "new PR commit")
    latest = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", "HEAD:feature")
    git(repo, "checkout", "feature")
    from unittest.mock import AsyncMock

    monkeypatch.setattr(worktree, "_gh", AsyncMock(return_value=(0, "https://example/pr/1", "")))
    if conflicting:
        with pytest.raises(worktree.IntegrationRequired):
            await worktree.open_pr(str(repo), "feature", title="recovery")
        context = await worktree.prepare_integration(str(repo), "feature", "main")
        assert context["conflict_paths"] == ["resolution"]
        assert context["integration_sha"] == latest
        # A restarted board resumes the remote-head merge; the coder only edits.
        (repo / "resolution").write_text("unpublished work\nremote resolution\n")
    await worktree.finish_integration(str(repo), "feature", "main")
    await worktree.open_pr(str(repo), "feature", title="recovery")
    for sha in (local, latest, git(repo, "rev-parse", "origin/main")):
        git(repo, "merge-base", "--is-ancestor", sha, "HEAD")
    assert "unpublished work" in (repo / "resolution").read_text()
    assert (repo / "new-test").read_text() == "new remote coverage\n"
    assert not git(repo, "ls-files", "--unmerged")
    assert git(repo, "ls-remote", "origin", "refs/heads/feature").split()[0] == git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("state", ["present", "deleted", "unreachable"])
async def test_recovery_round_start_survives_a_deleted_branch(history, state):
    repo = history
    head = git(repo, "rev-parse", "origin/feature")
    if state == "deleted":
        git(git(repo, "remote", "get-url", "origin"), "update-ref", "-d", "refs/heads/feature")
    if state == "unreachable":
        git(repo, "remote", "set-url", "origin", str(repo / "unavailable"))
        with pytest.raises(worktree.IntegrationRequired, match="fetch failed"):
            await worktree.recovery_round_start(str(repo), "feature")
        return
    expected = head if state == "present" else ""
    assert await worktree.recovery_round_start(str(repo), "feature") == expected


async def test_deleted_pr_branch_recovers_preserved_work_instead_of_blocking(history, monkeypatch):
    """PR #720 review: a recovery worktree whose remote PR branch was deleted must not
    block every later dispatch; its preserved work continues and is published again."""
    from pathlib import Path
    from unittest.mock import AsyncMock

    from tests.test_loop import FEATURE, _drive_with

    repo = history
    feature = {**FEATURE, "pr_url": "https://example/pr/9"}
    fid, title = feature["id"], feature["title"]
    branch = worktree.branch_name(fid, title)
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    sibling(repo, "a")
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    # The coder resolved the conflict and kept unique work, but it was never published.
    (Path(path) / "a").write_text("feature coverage\nsibling a coverage\n")
    (Path(path) / "unique").write_text("only copy\n")
    await worktree.finish_integration(path, branch, "main")
    git(git(repo, "remote", "get-url", "origin"), "update-ref", "-d", f"refs/heads/{branch}")
    assert await worktree.pending_integration(str(repo), ".worktrees", fid, title)
    opened = AsyncMock(return_value=feature["pr_url"])
    prompts = []

    def seed(loop):
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")

    async def dispatch(coder, wt, prompt, **kwargs):
        prompts.append(prompt)
        assert wt == path  # the preserved checkout, not a fresh tree off main
        (Path(wt) / "more").write_text("follow-up\n")
        return "## Summary\nContinued recovery"

    loop, store = await _drive_with(
        monkeypatch,
        feature=feature,
        seed=seed,
        dispatch=dispatch,
        open_pr=opened,
        gate=AsyncMock(return_value=None),
        cfg={"coder": "proto", "local_gate_cmd": "check"},
    )
    assert len(prompts) == 1
    assert "flag_blocked" not in store.names()
    opened.assert_awaited_once()
    assert (Path(opened.call_args.args[0]) / "unique").read_text() == "only copy\n"


@pytest.mark.parametrize("preserved", [False, True])
async def test_auto_unblock_deleted_pr_branch(history, monkeypatch, preserved):
    from pathlib import Path
    from project_board.loop import BoardLoop
    from tests.test_loop import _BlockedStore, _blocked

    repo = history
    fid, title = "bd-deleted-block", "Deleted blocked branch"
    branch = worktree.branch_name(fid, title)
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    if preserved:
        (Path(path) / "unique").write_text("only copy\n")
        await worktree.finish_integration(path, branch, "main")
    else:
        git(repo, "worktree", "remove", path)
    git(git(repo, "remote", "get-url", "origin"), "update-ref", "-d", f"refs/heads/{branch}")
    row = {**_blocked(fid, "merge-conflict", title=title), "pr_url": "https://example/pr/1"}
    blocked = _BlockedStore([row])
    loop = BoardLoop({"coder": "proto"})
    monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
    monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
    await loop._recover_blocked(blocked)
    assert blocked.cleared == [fid] and blocked.requeued == [fid]
    resumed, _ = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    if preserved:
        assert resumed == path
        assert (Path(resumed) / "unique").read_text() == "only copy\n"
    else:
        assert git(resumed, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")


async def test_pending_base_conflict_then_remote_conflict(history, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    repo = history
    pinned_base = sibling(repo, "a")
    await worktree.prepare_integration(str(repo), "feature", "main")
    other = tmp_path / "remote-editor"
    git(repo, "worktree", "add", "-b", "remote-editor", str(other), "origin/feature")
    (other / "b").write_text("remote coverage\n")
    git(other, "commit", "-am", "concurrent PR edit")
    remote_head = git(other, "rev-parse", "HEAD")
    git(other, "push", "origin", "HEAD:feature")
    (repo / "a").write_text("feature coverage\nsibling a coverage\n")
    (repo / "b").write_text("feature coverage\nlocal coverage\n")
    with pytest.raises(worktree.IntegrationRequired, match="advanced"):
        await worktree.finish_integration(str(repo), "feature", "main")
    context = await worktree.prepare_integration(str(repo), "feature", "main")
    assert context["base_sha"] == pinned_base
    assert context["integration_sha"] == remote_head
    assert context["conflict_paths"] == ["b"]
    (repo / "b").write_text("feature coverage\nlocal coverage\nremote coverage\n")
    monkeypatch.setattr(worktree, "_gh", AsyncMock(return_value=(0, "https://example/pr/1", "")))
    await worktree.finish_integration(str(repo), "feature", "main")
    await worktree.request_requirement_followup(str(repo), "feature")
    await worktree.open_pr(str(repo), "feature", title="recovered")
    assert not (await worktree.recovery_context(str(repo), "feature"))["requirement_followup_pending"]
    for sha in (pinned_base, remote_head):
        git(repo, "merge-base", "--is-ancestor", sha, "HEAD")
    assert (repo / "a").read_text() == "feature coverage\nsibling a coverage\n"
    assert (repo / "b").read_text() == "feature coverage\nlocal coverage\nremote coverage\n"
    git(repo, "merge-tree", "--write-tree", "origin/main", "HEAD")


@pytest.mark.parametrize("moving_branch", ["main", "feature"])
async def test_remote_movement_after_verification_never_integrates_at_publication(history, monkeypatch, moving_branch):
    from unittest.mock import AsyncMock

    repo = history
    await worktree.prepare_integration(str(repo), "feature", "main")
    (repo / "local").write_text("unpublished\n")
    await worktree.finish_integration(str(repo), "feature", "main")
    verified = git(repo, "rev-parse", "HEAD")
    # A clean merge can still invalidate the behavior checked by local gates.
    git(repo, "checkout", "-b", "racing-editor", f"origin/{moving_branch}")
    (repo / "remote").write_text("concurrent incompatible behavior\n")
    git(repo, "add", "remote")
    git(repo, "commit", "-m", "racing remote edit")
    advanced = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", f"HEAD:{moving_branch}")
    git(repo, "checkout", "feature")
    remote_before = git(repo, "ls-remote", "origin", "refs/heads/feature")
    gh = AsyncMock(return_value=(0, "https://example/pr/1", ""))
    monkeypatch.setattr(worktree, "_gh", gh)
    with pytest.raises(worktree.IntegrationRequired, match="advanced"):
        await worktree.open_pr(str(repo), "feature", title="recovery")
    gh.assert_not_awaited()
    assert git(repo, "rev-parse", "HEAD") == verified
    assert git(repo, "ls-remote", "origin", "refs/heads/feature") == remote_before
    assert not (repo / "remote").exists()
    # Next recovery round integrates before verification, then can publish.
    await worktree.prepare_integration(str(repo), "feature", "main")
    await worktree.finish_integration(str(repo), "feature", "main")
    git(repo, "merge-base", "--is-ancestor", advanced, "HEAD")
    assert (repo / "local").read_text() == "unpublished\n"
    assert (repo / "remote").read_text() == "concurrent incompatible behavior\n"
    await worktree.open_pr(str(repo), "feature", title="recovery")
    gh.assert_awaited_once()


@pytest.mark.parametrize("driver_selection", ["attribute", "default"])
async def test_failed_custom_driver_requires_edit_across_restart(history, driver_selection):
    repo = history
    if driver_selection == "attribute":
        (repo / ".gitattributes").write_text("a merge=partial\n")
        git(repo, "add", ".gitattributes")
        git(repo, "commit", "-m", "custom driver")
    else:
        git(repo, "config", "merge.default", "partial")
    git(repo, "config", "merge.partial.driver", "printf 'partial output\\n' > %A; exit 1")
    sibling(repo, "a")
    if driver_selection == "default":
        # Resume a merge started before the board pinned the default driver.
        assert not (repo / ".gitattributes").exists()
        result = subprocess.run(
            ["git", "-C", str(repo), "merge", "--no-commit", "--no-ff", "origin/main"], capture_output=True
        )
        assert result.returncode == 1
    context = await worktree.prepare_integration(str(repo), "feature", "main")
    assert context["conflict_paths"] == ["a"]
    assert (repo / "a").read_text() == "partial output\n"
    head = git(repo, "rev-parse", "HEAD")
    index = git(repo, "ls-files", "--unmerged")
    for _ in range(2):
        await worktree.prepare_integration(str(repo), "feature", "main")
        with pytest.raises(worktree.IntegrationRequired, match="markerless"):
            await worktree.commit_worktree(str(repo), "must not accept driver output")
        assert git(repo, "rev-parse", "HEAD") == head
        assert git(repo, "ls-files", "--unmerged") == index
    (repo / "a").write_text("feature coverage\nsibling a coverage\n")
    # Restart after edits must retain the original snapshot, not rebaseline it.
    await worktree.prepare_integration(str(repo), "feature", "main")
    await worktree.finish_integration(str(repo), "feature", "main")
    git(repo, "merge-base", "--is-ancestor", "origin/main", "HEAD")
    assert not git(repo, "ls-files", "--unmerged")
    assert (repo / "a").read_text() == "feature coverage\nsibling a coverage\n"


@pytest.mark.parametrize("moving_branch", ["main", "feature"])
async def test_drive_remote_movement_in_gate_blocks_review(history, monkeypatch, moving_branch):
    from pathlib import Path
    from unittest.mock import AsyncMock

    from tests.test_loop import FEATURE, _drive_with

    repo = history
    feature = {**FEATURE, "pr_url": "https://example/pr/7"}
    branch = worktree.branch_name(feature["id"], feature["title"])
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    git(repo, "checkout", "main")
    create, publish = worktree.create_worktree, worktree.open_pr
    checked = []
    gh = AsyncMock(side_effect=AssertionError("unverified candidate must not publish"))
    monkeypatch.setattr(worktree, "_gh", gh)

    def seed(loop):
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(worktree, "create_worktree", create)

    async def dispatch(coder, path, prompt, **kwargs):
        (Path(path) / "unique").write_text("preserved recovery work\n")
        return "## Summary\nRecovered"

    async def gate(path, feature):
        checked.append((path, git(path, "rev-parse", "HEAD")))
        target = "main" if moving_branch == "main" else branch
        git(repo, "checkout", "-b", "concurrent-editor", f"origin/{target}")
        (repo / "incompatible").write_text("invalidates the verified behavior\n")
        git(repo, "add", "incompatible")
        git(repo, "commit", "-m", "concurrent change during verification")
        git(repo, "push", "origin", f"HEAD:{target}")
        return None

    loop, store = await _drive_with(
        monkeypatch,
        feature=feature,
        seed=seed,
        dispatch=dispatch,
        open_pr=publish,
        gate=gate,
        cfg={"coder": "proto", "local_gate_cmd": "check"},
    )
    assert len(checked) == 1
    path, verified = checked[0]
    assert git(path, "rev-parse", "HEAD") == verified
    assert (Path(path) / "unique").read_text() == "preserved recovery work\n"
    assert not (Path(path) / "incompatible").exists()
    assert "flag_blocked" in store.names()
    assert "open_review" not in store.names()
    assert not loop._ci_fix_attempts and not loop._review_fix_attempts
    gh.assert_not_awaited()


async def test_remote_head_race_during_publication_still_blocks(history, monkeypatch):
    repo = history
    await worktree.prepare_integration(str(repo), "feature", "main")
    (repo / "local").write_text("unpublished\n")
    await worktree.finish_integration(str(repo), "feature", "main")
    local = git(repo, "rev-parse", "HEAD")
    commit = worktree.commit_worktree

    async def advance_after_commit(path, message):
        await commit(path, message)
        git(repo, "checkout", "-b", "racing-editor", "origin/feature")
        (repo / "remote").write_text("concurrent\n")
        git(repo, "add", "remote")
        git(repo, "commit", "-m", "racing remote edit")
        git(repo, "push", "origin", "HEAD:feature")
        git(repo, "checkout", "feature")

    monkeypatch.setattr(worktree, "commit_worktree", advance_after_commit)
    with pytest.raises(worktree.IntegrationRequired, match="PR head advanced"):
        await worktree.open_pr(str(repo), "feature", title="recovery")
    assert git(repo, "rev-parse", "HEAD") == local
    assert git(repo, "ls-remote", "origin", "refs/heads/feature").split()[0] != local


@pytest.mark.parametrize("moving_branch", ["main", "feature"])
async def test_board_pins_text_driver_and_requires_resolution(history, moving_branch):
    repo = history
    git(repo, "config", "merge.default", "partial")
    git(repo, "config", "merge.partial.driver", "printf 'partial output\\n' > %A; exit 1")
    if moving_branch == "main":
        sibling(repo, "a")
    else:
        git(repo, "checkout", "-b", "remote-editor", "origin/feature")
        (repo / "a").write_text("remote coverage\n")
        git(repo, "commit", "-am", "remote edit")
        git(repo, "push", "origin", "HEAD:feature")
        git(repo, "checkout", "feature")
        (repo / "a").write_text("local coverage\n")
        git(repo, "commit", "-am", "local edit")
    context = await worktree.prepare_integration(str(repo), "feature", "main")
    assert context["conflict_paths"] == ["a"]
    assert "<<<<<<<" in (repo / "a").read_text()
    head = git(repo, "rev-parse", "HEAD")
    index = git(repo, "ls-files", "--unmerged")
    with pytest.raises(worktree.IntegrationRequired, match="explicit resolution"):
        await worktree.commit_worktree(str(repo), "untouched text")
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "ls-files", "--unmerged") == index
    # An edit alone is insufficient while text markers remain.
    with (repo / "a").open("a") as stream:
        stream.write("extra coverage\n")
    with pytest.raises(worktree.IntegrationRequired, match="Unresolved merge content"):
        await worktree.commit_worktree(str(repo), "still has markers")
    (repo / "a").write_text("feature coverage\nsibling coverage\nremote coverage\nlocal coverage\n")
    await worktree.commit_worktree(str(repo), "resolved text")
    assert not git(repo, "ls-files", "--unmerged")
    git(repo, "merge-base", "--is-ancestor", context["integration_sha"], "HEAD")


async def test_interrupted_text_merge_without_snapshot_requires_another_edit(history):
    repo = history
    sibling(repo, "a")
    result = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-commit", "--no-ff", "origin/main"], capture_output=True
    )
    assert result.returncode == 1
    # Resolution predates the board's first snapshot and cannot be trusted yet.
    (repo / "a").write_text("feature coverage\nsibling a coverage\n")
    with pytest.raises(worktree.IntegrationRequired, match="explicit resolution"):
        await worktree.commit_worktree(str(repo), "missing snapshot")
    (repo / "a").write_text("feature coverage\nsibling a coverage\nextra coverage\n")
    await worktree.commit_worktree(str(repo), "explicit resolution after snapshot")
    assert not git(repo, "ls-files", "--unmerged")
    git(repo, "merge-base", "--is-ancestor", "origin/main", "HEAD")


@pytest.mark.parametrize("local_work", ["none", "edit", "staged", "commit"])
@pytest.mark.parametrize("rewound", [False, True])
async def test_published_recovery_remote_rewind_does_not_restore_removed_commits(
    history, monkeypatch, local_work, rewound
):
    from pathlib import Path
    from unittest.mock import AsyncMock

    repo = history
    fid, title = "bd-rewind", "Rewound recovery"
    branch = worktree.branch_name(fid, title)
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    original = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    (Path(path) / "resolution").write_text("published resolution\n")
    await worktree.finish_integration(path, branch, "main")
    published = git(path, "rev-parse", "HEAD")
    # A successful push remains published even when subsequent GitHub work fails.
    monkeypatch.setattr(worktree, "_gh", AsyncMock(side_effect=RuntimeError("GitHub unavailable")))
    with pytest.raises(RuntimeError, match="GitHub unavailable"):
        await worktree.open_pr(path, branch, title=title)
    assert (await worktree.recovery_context(path, branch))["published_head_sha"] == published
    remote = git(repo, "remote", "get-url", "origin")
    if rewound:
        git(remote, "update-ref", f"refs/heads/{branch}", original)
    if local_work != "none":
        (Path(path) / "unique").write_text("unpublished local work\n")
        if local_work == "staged":
            git(path, "add", "unique")
        if local_work == "commit":
            await worktree.commit_worktree(path, "unique local work")
        if not rewound:
            assert await worktree.pending_integration(str(repo), ".worktrees", fid, title) == (path, branch)
            await worktree.prepare_integration(path, branch, "main")
            await worktree.finish_integration(path, branch, "main")
            with pytest.raises(RuntimeError, match="GitHub unavailable"):
                await worktree.open_pr(path, branch, title=title)
            assert git(remote, "rev-parse", f"refs/heads/{branch}") == git(path, "rev-parse", "HEAD")
            assert (Path(path) / "unique").read_text() == "unpublished local work\n"
            return
        local_head = git(path, "rev-parse", "HEAD")
        local_status = git(path, "status", "--porcelain")
        # Restart discovery and direct reuse must both refuse the stale ancestry.
        # Repeated attempts preserve the sole copy of local work, including its index.
        for _ in range(2):
            with pytest.raises(worktree.IntegrationRequired, match="history was rewritten"):
                await worktree.pending_integration(str(repo), ".worktrees", fid, title)
            with pytest.raises(worktree.IntegrationRequired, match="history was rewritten"):
                await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
            with pytest.raises(worktree.IntegrationRequired, match="history was rewritten"):
                await worktree.prepare_integration(path, branch, "main")
            assert git(path, "rev-parse", "HEAD") == local_head
            assert git(path, "status", "--porcelain") == local_status
        # Publication can race a remote rewind after verification, or bypass discovery.
        # Being a descendant of the rewound remote is insufficient authorization.
        with pytest.raises(worktree.IntegrationRequired, match="history was rewritten"):
            await worktree.open_pr(path, branch, title=title)
        assert git(remote, "rev-parse", f"refs/heads/{branch}") == original
        assert (Path(path) / "unique").read_text() == "unpublished local work\n"
        assert git(path, "merge-base", "--is-ancestor", local_head, "HEAD") == ""
        assert (await worktree.recovery_context(path, branch))["published_head_sha"] == published
        return
    # No in-memory state survives: journal alone must distinguish publication.
    assert await worktree.pending_integration(str(repo), ".worktrees", fid, title) is None
    path, _ = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    assert git(path, "rev-parse", "HEAD") == (original if rewound else published)
    assert (Path(path) / "resolution").exists() is not rewound


@pytest.mark.parametrize("resolution", ["delete", "replace"])
async def test_gitlink_conflict_accepts_explicit_edit_only_resolution(history, resolution):
    repo = history
    common = git(repo, "rev-parse", "main")
    tree = git(repo, "rev-parse", "main^{tree}")
    ours = git(repo, "commit-tree", tree, "-p", common, "-m", "submodule feature")
    theirs = git(repo, "commit-tree", tree, "-p", common, "-m", "submodule sibling")
    git(repo, "checkout", "main")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{common},module")
    git(repo, "commit", "-m", "shared submodule")
    git(repo, "checkout", "feature")
    git(repo, "merge", "main", "-m", "shared submodule")
    git(repo, "update-index", "--cacheinfo", f"160000,{ours},module")
    git(repo, "commit", "-m", "feature submodule")
    git(repo, "push", "origin", "feature")
    git(repo, "checkout", "main")
    git(repo, "update-index", "--cacheinfo", f"160000,{theirs},module")
    git(repo, "commit", "-m", "sibling submodule")
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "feature")
    (repo / "module").mkdir(exist_ok=True)
    context = await worktree.prepare_integration(str(repo), "feature", "main")
    assert context["conflict_paths"] == ["module"]
    with pytest.raises(worktree.IntegrationRequired, match="explicit resolution"):
        await worktree.commit_worktree(str(repo), "untouched gitlink")
    assert git(repo, "ls-files", "--unmerged")
    (repo / "module").rmdir()
    if resolution == "replace":
        (repo / "module").write_text("replacement for submodule\n")
    await worktree.finish_integration(str(repo), "feature", "main")
    assert not git(repo, "ls-files", "--unmerged")
    assert git(repo, "merge-base", "--is-ancestor", context["base_sha"], "HEAD") == ""
    if resolution == "delete":
        assert not git(repo, "ls-files", "module")
    else:
        assert git(repo, "ls-files", "--stage", "module").startswith("100644")


@pytest.mark.parametrize("staged", [False, True])
async def test_retained_edits_participate_in_advancing_base_merge(history, staged):
    repo = history
    base = sibling(repo, "a")
    original = git(repo, "rev-parse", "HEAD")
    (repo / "a").write_text("feature coverage\nretained coverage\n")
    if staged:
        git(repo, "add", "a")
    context = await worktree.prepare_integration(str(repo), "feature", "main")
    assert context["conflict_paths"] == ["a"]
    assert context["head_sha"] != original
    assert git(repo, "show", "HEAD:a") == "feature coverage\nretained coverage"
    (repo / "a").write_text("feature coverage\nretained coverage\nsibling a coverage\n")
    await worktree.finish_integration(str(repo), "feature", "main")
    git(repo, "merge-base", "--is-ancestor", base, "HEAD")
    git(repo, "merge-tree", "--write-tree", "origin/main", "HEAD")
    assert "retained coverage" in (repo / "a").read_text()


async def test_shutdown_keeps_exact_recovery_index_for_restart(history):
    from pathlib import Path
    from project_board.loop import BoardLoop

    repo = history
    sibling(repo, "a")
    fid, title = "bd-stop", "Recovery shutdown"
    branch = worktree.branch_name(fid, title)
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    index = git(path, "ls-files", "--unmerged")
    merge_head = git(path, "rev-parse", "MERGE_HEAD")
    (Path(path) / "a").write_text("feature coverage\nsibling a coverage\n")
    loop = BoardLoop({"coder": "proto"})
    loop._inflight[fid] = (str(repo), path, branch)
    await loop.stop()
    assert git(path, "ls-files", "--unmerged") == index
    assert git(path, "rev-parse", "MERGE_HEAD") == merge_head
    assert await worktree.pending_integration(str(repo), ".worktrees", fid, title) == (path, branch)
    await worktree.prepare_integration(path, branch, "main")
    await worktree.finish_integration(path, branch, "main")
    git(path, "merge-tree", "--write-tree", "origin/main", "HEAD")
    assert (Path(path) / "a").read_text() == "feature coverage\nsibling a coverage\n"


@pytest.mark.parametrize("provider_retry", [None, "backoff", "rotation"])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("edits", [False, True])
async def test_clean_board_merge_does_not_count_as_coder_fix(history, monkeypatch, edits, restart, provider_retry):
    from pathlib import Path
    from unittest.mock import AsyncMock
    from tests.test_loop import FEATURE, FakeLoopStore, _drive_with

    repo = history
    feature = {**FEATURE, "pr_url": "https://example/pr/7"}
    branch = worktree.branch_name(feature["id"], feature["title"])
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    git(repo, "checkout", "main")
    (repo / "sibling").write_text("sibling coverage\n")
    git(repo, "add", "sibling")
    git(repo, "commit", "-m", "clean sibling change")
    git(repo, "push", "origin", "main")
    create = worktree.create_worktree
    opened = AsyncMock(return_value=feature["pr_url"])
    if restart:
        path, _ = await create(str(repo), "main", feature["id"], title=feature["title"], resume=True, integrate=True)
        await worktree.finish_integration(path, branch, "main")

    store = FakeLoopStore()
    monkeypatch.setattr(store, "escalate", lambda *_: None, raising=False)
    providers = []
    backoff = AsyncMock()
    monkeypatch.setattr("project_board.loop.asyncio.sleep", backoff)
    monkeypatch.setattr("project_board.loop.drive._next_rung_cursor", lambda: 0)

    def seed(loop):
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(loop, "_empty_result_signals", lambda _: (True, "end_turn"))
        monkeypatch.setattr(worktree, "create_worktree", create)
        monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: name)
        loop._ci_feedback[feature["id"]] = "Fix the rejected implementation"

    async def dispatch(coder, path, prompt, **kwargs):
        providers.append(coder)
        if provider_retry and len(providers) == 1:
            raise worktree.WorktreeError("coder dispatch failed: 429 rate limit exceeded")
        if not restart:
            assert git(path, "rev-parse", "MERGE_HEAD")
        if edits:
            (Path(path) / "fix").write_text("actual fix\n")
        return "## Summary\nDone"

    _, store = await _drive_with(
        monkeypatch,
        feature=feature,
        store=store,
        seed=seed,
        dispatch=dispatch,
        open_pr=opened,
        cfg={
            "coder": "proto",
            **(
                {"coders": {"fast": ["proto", "alternate"], "reasoning": ["stronger"]}}
                if provider_retry == "rotation"
                else {}
            ),
        },
    )
    assert len(providers) == (2 if provider_retry else 1)
    if provider_retry:
        assert (providers[0] != providers[1]) is (provider_retry == "rotation")
        assert bool(backoff.await_count) is (provider_retry == "backoff")
    assert opened.await_count == int(edits)
    assert ("open_review" in store.names()) is edits
    if not edits:
        assert any(c[0] == "flag_blocked" and "fix round produced no commit" in c[2] for c in store.calls)


async def test_recovery_edit_snapshot_tracks_content_and_ignores_board_scratch(history):
    repo = history
    await worktree.prepare_integration(str(repo), "feature", "main")
    (repo / "untracked").write_text("first\n")
    before = await worktree.recovery_edit_state(str(repo))
    (repo / ".proto").mkdir()
    (repo / ".proto" / "session").write_text("board scratch\n")
    assert await worktree.recovery_edit_state(str(repo)) == before
    (repo / "untracked").write_text("second\n")
    assert await worktree.recovery_edit_state(str(repo)) != before
    (repo / "untracked").write_text("first\n")
    (repo / "a").unlink()
    assert await worktree.recovery_edit_state(str(repo)) != before


@pytest.mark.parametrize("provider_retry", [None, "backoff", "rotation"])
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("disposition", ["done", "declined — outside scope", None, "declined"])
async def test_existing_pr_ledger_only_recovery_reaches_completion_gate(
    history, monkeypatch, disposition, partial, provider_retry
):
    from pathlib import Path
    from unittest.mock import AsyncMock
    from tests.test_loop import FEATURE, FakeLoopStore, _drive_with

    repo = history
    sibling(repo, "a")
    feature = {
        **FEATURE,
        "pr_url": "https://example/pr/7",
        "requirements": [
            {"id": "r1", "text": "preserve coverage", "status": "open"},
            {"id": "r2", "text": "other coverage", "status": "open"},
        ],
    }
    branch = worktree.branch_name(feature["id"], feature["title"])
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    git(repo, "checkout", "main")
    create = worktree.create_worktree
    opened = AsyncMock(return_value=feature["pr_url"])
    prompts = []
    states = []
    providers = []
    store = FakeLoopStore()
    monkeypatch.setattr(store, "escalate", lambda *_: None, raising=False)
    backoff = AsyncMock()
    monkeypatch.setattr("project_board.loop.asyncio.sleep", backoff)
    monkeypatch.setattr("project_board.loop.drive._next_rung_cursor", lambda: 0)

    def seed(loop):
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(loop, "_empty_result_signals", lambda _: (True, "end_turn"))
        monkeypatch.setattr(worktree, "create_worktree", create)
        monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: name)
        loop._ci_feedback[feature["id"]] = "Resolve the conflict"

    async def dispatch(coder, path, prompt, **kwargs):
        providers.append(coder)
        if provider_retry and len(providers) == 2:
            assert ("requirement items are still OPEN" if partial else "do NOT edit") in prompt
            raise worktree.WorktreeError("coder dispatch failed: 429 rate limit exceeded")
        prompts.append(prompt)
        if len(prompts) == 1:
            (Path(path) / "a").write_text("feature coverage\nsibling a coverage\n")
        else:
            states.append(await worktree.recovery_edit_state(path))
            if len(prompts) == 2:
                assert ("requirement items are still OPEN" if partial else "do NOT edit") in prompt
                if disposition:
                    return f"## Requirements\n- r1: {disposition}\n- r2: done\n\n## Summary\nPreserved coverage"
            else:
                assert "requirement items are still OPEN" in prompt
        return ("## Requirements\n- r2: done\n\n" if partial else "") + "## Summary\nPreserved coverage"

    _, store = await _drive_with(
        monkeypatch,
        feature=feature,
        store=store,
        seed=seed,
        dispatch=dispatch,
        open_pr=opened,
        cfg={
            "coder": "proto",
            "goal_fix_max": 1,
            **(
                {"coders": {"fast": ["proto", "alternate"], "reasoning": ["stronger"]}}
                if provider_retry == "rotation"
                else {}
            ),
        },
    )
    if provider_retry:
        assert len(providers) == len(prompts) + 1
        assert (providers[1] != providers[2]) is (provider_retry == "rotation")
        assert bool(backoff.await_count) is (provider_retry == "backoff")
    if disposition in ("done", "declined — outside scope"):
        assert len(prompts) == 2
        opened.assert_awaited_once()
        path = opened.call_args.args[0]
        assert await worktree.recovery_edit_state(path) == states[0]
        git(path, "merge-base", "--is-ancestor", "origin/main", "HEAD")
        git(path, "merge-tree", "--write-tree", "origin/main", "HEAD")
        assert (Path(path) / "a").read_text() == "feature coverage\nsibling a coverage\n"
        assert feature["requirements"][0]["status"] == disposition.split(" — ")[0]
        writes = [c for c in store.calls if c[0] == "set_requirements"]
        assert writes[-1][2] == feature["requirements"]
        assert "open_review" in store.names()
    else:
        # Invalid/repeated dispositions exhaust the requirement budget.
        assert len(prompts) <= 4
        opened.assert_not_awaited()
        assert any(c[0] == "flag_blocked" and "requirements unresolved" in c[2] for c in store.calls)


@pytest.mark.parametrize("provider_retry", [None, "backoff", "rotation"])
@pytest.mark.parametrize("committed", [False, True, "unproven", "equivalent"])
async def test_drive_restart_publishes_preserved_resolution(history, monkeypatch, committed, provider_retry):
    from pathlib import Path
    from unittest.mock import AsyncMock
    from tests.test_loop import FEATURE, _drive_with

    repo = history
    base = sibling(repo, "a")
    feature = {**FEATURE, "pr_url": "https://example/pr/7"}
    branch = worktree.branch_name(feature["id"], feature["title"])
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    remote = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(
        str(repo), "main", feature["id"], title=feature["title"], resume=True, integrate=True
    )
    (Path(path) / "a").write_text("feature coverage\nsibling a coverage\n")
    if committed == "unproven":
        git(path, "add", "a")
        git(path, "commit", "-m", "arbitrary local divergence without board provenance")
    elif committed:
        await worktree.finish_integration(path, branch, "main")
        if committed == "equivalent":
            # Another publisher supplied the same resolution with a different SHA.
            equivalent = git(
                path,
                "commit-tree",
                "HEAD^{tree}",
                "-p",
                remote,
                "-p",
                base,
                "-m",
                "concurrently published equivalent resolution",
            )
            git(path, "push", "origin", f"{equivalent}:refs/heads/{branch}")
    verify = AsyncMock(return_value=None)
    gate = AsyncMock(return_value=None)

    async def publish(path, branch, **kwargs):
        git(path, "merge-base", "--is-ancestor", remote, "HEAD")
        git(path, "merge-base", "--is-ancestor", base, "HEAD")
        git(path, "merge-tree", "--write-tree", "origin/main", "HEAD")
        assert (Path(path) / "a").read_text() == "feature coverage\nsibling a coverage\n"
        git(path, "push", "origin", branch)
        return feature["pr_url"]

    opened = AsyncMock(side_effect=publish)

    providers = []
    backoff = AsyncMock()
    monkeypatch.setattr("project_board.loop.asyncio.sleep", backoff)
    monkeypatch.setattr("project_board.loop.drive._next_rung_cursor", lambda: 0)

    def seed(loop):
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(loop, "_verify_goal", verify)
        monkeypatch.setattr(loop, "_empty_result_signals", lambda _: (True, "end_turn"))
        monkeypatch.setattr(loop, "_resolve_delegate", lambda name, expect: name)
        loop.goal_verify = True

    async def dispatch(coder, resumed_path, *args, **kwargs):
        assert resumed_path == path
        providers.append(coder)
        if provider_retry and len(providers) == 1:
            raise worktree.WorktreeError("coder dispatch failed: 429 rate limit exceeded")
        return "## Summary\nRetained resolution"

    _, store = await _drive_with(
        monkeypatch,
        feature=feature,
        seed=seed,
        dispatch=dispatch,
        open_pr=opened,
        gate=gate,
        cfg={
            "coder": "proto",
            **(
                {"coders": {"fast": ["proto", "alternate"], "reasoning": ["stronger"]}}
                if provider_retry == "rotation"
                else {}
            ),
        },
    )
    assert len(providers) == (2 if provider_retry else 1)
    if provider_retry:
        assert (providers[0] != providers[1]) is (provider_retry == "rotation")
        assert bool(backoff.await_count) is (provider_retry == "backoff")
    if committed in ("unproven", "equivalent"):
        verify.assert_not_awaited()
        gate.assert_not_awaited()
        opened.assert_not_awaited()
        assert "flag_blocked" in store.names()
        retained = git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/stranded/").splitlines()
        if committed == "unproven":
            assert retained
            assert git(repo, "show", f"{retained[0]}:a") == "feature coverage\nsibling a coverage"
        return
    verify.assert_awaited_once()
    gate.assert_awaited_once()
    opened.assert_awaited_once()
    assert "open_review" in store.names()
    assert not await worktree.preserved_recovery_progress(path, branch)


async def test_requirement_progress_does_not_exempt_later_goal_repair(history, monkeypatch):
    from pathlib import Path
    from unittest.mock import AsyncMock
    from tests.test_loop import FEATURE, _drive_with

    repo = history
    feature = {
        **FEATURE,
        "pr_url": "https://example/pr/7",
        "requirements": [
            {"id": "r1", "text": "first", "status": "open"},
            {"id": "r2", "text": "second", "status": "open"},
        ],
    }
    branch = worktree.branch_name(feature["id"], feature["title"])
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    git(repo, "checkout", "main")
    create = worktree.create_worktree
    opened = AsyncMock(return_value=feature["pr_url"])
    verify = AsyncMock(side_effect=[None, "Missing test"])
    replies = []

    def seed(loop):
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(worktree, "create_worktree", create)
        monkeypatch.setattr(loop, "_verify_goal", verify)
        monkeypatch.setattr(loop, "_empty_result_signals", lambda _: (True, "end_turn"))
        loop.goal_verify = True

    async def dispatch(coder, path, prompt, **kwargs):
        replies.append(prompt)
        if len(replies) == 1:
            (Path(path) / "fix").write_text("repair\n")
            return "## Requirements\n- r1: done\n## Summary\nRepair"
        return "## Requirements\n- r1: done\n- r2: done\n## Summary\nRepair"

    _, store = await _drive_with(
        monkeypatch,
        feature=feature,
        seed=seed,
        dispatch=dispatch,
        open_pr=opened,
        cfg={"coder": "proto", "goal_fix_max": 1},
    )
    assert len(replies) == 3
    assert verify.await_count == 2
    opened.assert_not_awaited()
    assert any(c[0] == "flag_blocked" and "fix round produced no commit" in c[2] for c in store.calls)


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("failed_check", ["goal", "gate"])
@pytest.mark.parametrize("repair_edits", [False, True])
async def test_preserved_resolution_does_not_exempt_new_gate_repair(
    history, monkeypatch, committed, failed_check, repair_edits
):
    from pathlib import Path
    from unittest.mock import AsyncMock
    from tests.test_loop import FEATURE, _drive_with

    repo = history
    base = sibling(repo, "a")
    feature = {**FEATURE, "pr_url": "https://example/pr/7"}
    branch = worktree.branch_name(feature["id"], feature["title"])
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    remote = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(
        str(repo), "main", feature["id"], title=feature["title"], resume=True, integrate=True
    )
    resolution = "feature coverage\nsibling a coverage\n"
    (Path(path) / "a").write_text(resolution)
    if committed:
        await worktree.finish_integration(path, branch, "main")

    async def check(path, feature):
        return None if (Path(path) / "gate-fix").exists() else "Missing regression test"

    gate = AsyncMock(side_effect=check if failed_check == "gate" else None, return_value=None)

    async def verify_goal(feature, path, base, result):
        return await check(path, feature) if failed_check == "goal" else None

    verify = AsyncMock(side_effect=verify_goal)
    backoff = AsyncMock()
    monkeypatch.setattr("project_board.loop.asyncio.sleep", backoff)
    attempts = []

    def seed(loop):
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(loop, "_verify_goal", verify)
        monkeypatch.setattr(loop, "_empty_result_signals", lambda _: (True, "end_turn"))
        loop.goal_verify = True

    async def dispatch(coder, resumed_path, prompt, **kwargs):
        assert resumed_path == path
        attempts.append(prompt)
        if len(attempts) == 2:
            # A provider failure during the NEW repair must not revive the
            # exemption already consumed by the successful resumed reply.
            raise worktree.WorktreeError("coder dispatch failed: 429 rate limit exceeded")
        if len(attempts) > 2 and repair_edits:
            (Path(path) / "gate-fix").write_text("regression coverage\n")
        return "## Summary\nRetained resolution"

    async def publish(path, branch, **kwargs):
        git(path, "merge-base", "--is-ancestor", remote, "HEAD")
        git(path, "merge-base", "--is-ancestor", base, "HEAD")
        git(path, "merge-tree", "--write-tree", "origin/main", "HEAD")
        assert (Path(path) / "a").read_text() == resolution
        assert (Path(path) / "gate-fix").exists()
        git(path, "push", "origin", branch)
        return feature["pr_url"]

    opened = AsyncMock(side_effect=publish)
    _, store = await _drive_with(
        monkeypatch,
        feature=feature,
        seed=seed,
        dispatch=dispatch,
        open_pr=opened,
        gate=gate,
        cfg={"coder": "proto", "local_gate_max": 1, "goal_fix_max": 1},
    )
    assert len(attempts) == 3
    assert "Missing regression test" in attempts[1]
    backoff.assert_awaited()
    if repair_edits:
        assert (Path(path) / "a").read_text() == resolution
        assert verify.await_count == 2
        opened.assert_awaited_once()
        assert "open_review" in store.names()
    else:
        verify.assert_awaited_once()
        assert gate.await_count == (1 if failed_check == "gate" else 0)
        opened.assert_not_awaited()
        assert any(c[0] == "flag_blocked" and "fix round produced no commit" in c[2] for c in store.calls)
        assert git(repo, "ls-remote", "origin", f"refs/heads/{branch}").split()[0] == remote
        retained = git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/stranded/").splitlines()
        assert len(retained) == 1
        assert git(repo, "show", f"{retained[0]}:a") == resolution.strip()
        git(repo, "merge-base", "--is-ancestor", remote, retained[0])
        git(repo, "merge-base", "--is-ancestor", base, retained[0])


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("repair_edits", [False, True])
@pytest.mark.parametrize("formatted", [False, True])
async def test_consumed_repair_survives_restart_during_gate_fix(
    history, monkeypatch, committed, repair_edits, formatted
):
    from pathlib import Path
    from unittest.mock import AsyncMock
    from tests.test_loop import FEATURE, FakeLoopStore, _drive_with

    class ProcessStopped(BaseException):
        pass

    repo = history
    base = sibling(repo, "a")
    feature = {**FEATURE, "pr_url": "https://example/pr/7", "labels": []}
    branch = worktree.branch_name(feature["id"], feature["title"])
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    remote = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(
        str(repo), "main", feature["id"], title=feature["title"], resume=True, integrate=True
    )
    resolution = "feature coverage\nsibling a coverage\n"
    (Path(path) / "a").write_text(resolution)
    if committed:
        await worktree.finish_integration(path, branch, "main")
    store = FakeLoopStore()

    def record_budget(fid, kind, n):
        feature["labels"] = [label for label in feature["labels"] if not label.startswith(f"budget:{kind}:")]
        feature["labels"].append(f"budget:{kind}:{n}")

    monkeypatch.setattr(store, "record_budget", record_budget, raising=False)
    loops = []

    async def fixups(path, feature):
        if formatted:
            (Path(path) / "b").write_text("feature coverage\nformatted\n")

    def seed(loop):
        loops.append(loop)
        monkeypatch.setattr(loop, "_run_fixups", fixups)
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(loop, "_empty_result_signals", lambda _: (True, "end_turn"))

    async def check(path, feature):
        return None if (Path(path) / "gate-fix").exists() else "Missing regression test"

    gate = AsyncMock(side_effect=check)
    attempts = []

    async def dispatch(coder, resumed_path, prompt, **kwargs):
        assert resumed_path == path
        attempts.append(prompt)
        if len(attempts) == 2:
            assert "Missing regression test" in prompt
            raise ProcessStopped()
        if len(attempts) == 3 and repair_edits:
            (Path(path) / "gate-fix").write_text("regression coverage\n")
        return "## Summary\nRetained resolution"

    async def publish(path, branch, **kwargs):
        git(path, "merge-base", "--is-ancestor", remote, "HEAD")
        git(path, "merge-base", "--is-ancestor", base, "HEAD")
        git(path, "merge-tree", "--write-tree", "origin/main", "HEAD")
        assert (Path(path) / "a").read_text() == resolution
        assert (Path(path) / "gate-fix").exists()
        git(path, "push", "origin", branch)
        return feature["pr_url"]

    opened = AsyncMock(side_effect=publish)
    kwargs = dict(
        feature=feature,
        store=store,
        seed=seed,
        dispatch=dispatch,
        open_pr=opened,
        gate=gate,
        cfg={"coder": "proto", "local_gate_max": 1},
    )
    with pytest.raises(ProcessStopped):
        await _drive_with(monkeypatch, **kwargs)
    assert "budget:gate-fix:1" in feature["labels"]
    context = await worktree.recovery_context(path, branch)
    assert context["consumed_repair"] == {
        "repair_head_sha": context["repair_head_sha"],
        "tree_sha": git(path, "rev-parse", "HEAD^{tree}"),
    }
    if formatted:
        assert git(path, "show", "HEAD:b") == "feature coverage\nformatted"
        assert not git(path, "status", "--porcelain")
    assert not await worktree.preserved_recovery_progress(path, branch)
    opened.assert_not_awaited()
    await _drive_with(monkeypatch, **kwargs)
    assert loops[0] is not loops[1]
    assert len(attempts) == 3
    if repair_edits:
        assert gate.await_count == 2
        opened.assert_awaited_once()
        assert "open_review" in store.names()
    else:
        gate.assert_awaited_once()
        opened.assert_not_awaited()
        assert any(c[0] == "flag_blocked" and "fix round produced no commit" in c[2] for c in store.calls)
        assert git(repo, "ls-remote", "origin", f"refs/heads/{branch}").split()[0] == remote


@pytest.mark.parametrize("stop_at", ["goal", "publication", "exhausted-gate", "ledger", "requirements"])
async def test_restart_retains_candidate_and_requirement_followup(history, monkeypatch, stop_at):
    from pathlib import Path
    from unittest.mock import AsyncMock
    from tests.test_loop import FEATURE, FakeLoopStore, _drive_with

    class ProcessStopped(BaseException):
        pass

    repo = history
    base = sibling(repo, "a")
    feature = {**FEATURE, "pr_url": "https://example/pr/7", "labels": []}
    followup = stop_at in ("ledger", "requirements")
    if followup:
        feature["requirements"] = [{"id": "r1", "text": "optional coverage", "status": "open"}]
    branch = worktree.branch_name(feature["id"], feature["title"])
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    remote = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(
        str(repo), "main", feature["id"], title=feature["title"], resume=True, integrate=True
    )
    resolution = "feature coverage\nsibling a coverage\n"
    (Path(path) / "a").write_text(resolution)
    await worktree.finish_integration(path, branch, "main")
    stopped = False
    attempts = []
    loops = []

    async def verify(*args):
        nonlocal stopped
        if stop_at == "goal" and not stopped:
            stopped = True
            raise ProcessStopped()
        return None

    def seed(loop):
        loops.append(loop)
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(loop, "_verify_goal", verify)
        monkeypatch.setattr(loop, "_empty_result_signals", lambda _: (True, "end_turn"))
        loop.goal_verify = True

    async def dispatch(coder, resumed_path, prompt, **kwargs):
        nonlocal stopped
        assert resumed_path == path
        attempts.append(prompt)
        if followup and len(attempts) == 2:
            # Also cover journals written by the previous version, which consumed
            # the candidate before requesting a requirement disposition.
            context = await worktree.recovery_context(path, branch)
            assert context["requirement_followup_pending"] is True
            context["consumed_repair"] = {
                "repair_head_sha": context["repair_head_sha"],
                "tree_sha": git(path, "rev-parse", "HEAD^{tree}"),
            }
            await worktree._record_recovery(path, branch, context)
            stopped = True
            raise ProcessStopped()
        if followup and stopped:
            return "## Requirements\n- r1: declined — optional coverage is outside scope\n## Summary\nRetained repair"
        heading = "## Requirements\n" if stop_at == "requirements" else ""
        return heading + "## Summary\nRetained repair"

    async def publish(path, branch, **kwargs):
        nonlocal stopped
        if stop_at in ("publication", "exhausted-gate") and not stopped:
            stopped = True
            raise ProcessStopped()
        git(path, "merge-base", "--is-ancestor", remote, "HEAD")
        git(path, "merge-base", "--is-ancestor", base, "HEAD")
        git(path, "merge-tree", "--write-tree", "origin/main", "HEAD")
        assert (Path(path) / "a").read_text() == resolution
        git(path, "push", "origin", branch)
        return feature["pr_url"]

    opened = AsyncMock(side_effect=publish)
    store = FakeLoopStore()
    kwargs = dict(
        feature=feature,
        store=store,
        seed=seed,
        dispatch=dispatch,
        open_pr=opened,
        gate=AsyncMock(return_value="Still failing" if stop_at == "exhausted-gate" else None),
        cfg={"coder": "proto", "goal_fix_max": 1, "local_gate_max": 0},
    )
    with pytest.raises(ProcessStopped):
        await _drive_with(monkeypatch, **kwargs)
    assert git(repo, "ls-remote", "origin", f"refs/heads/{branch}").split()[0] == remote
    if followup:
        assert not await worktree.preserved_recovery_progress(path, branch)
    else:
        assert await worktree.preserved_recovery_progress(path, branch)
    await _drive_with(monkeypatch, **kwargs)
    assert loops[0] is not loops[1]
    assert "open_review" in store.names()
    assert not any(c[0] == "flag_blocked" for c in store.calls)
    if followup:
        assert feature["requirements"][0]["status"] == "declined"


@pytest.mark.parametrize("interruption", ["none", "restart", "provider"])
@pytest.mark.parametrize("reply_kind", ["valid", "missing", "substantive-gap", "fresh-review"])
async def test_goal_followup_declaration(history, monkeypatch, interruption, reply_kind):
    from pathlib import Path
    from unittest.mock import AsyncMock
    from project_board.loop import BoardLoop
    from tests.test_loop import FEATURE, FakeLoopStore, _drive_with

    class ProcessStopped(BaseException):
        pass

    repo = history
    feature = {**FEATURE, "pr_url": "https://example/pr/7", "labels": [], "acceptance_criteria": "Keep behavior"}
    branch = worktree.branch_name(feature["id"], feature["title"])
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(
        str(repo), "main", feature["id"], title=feature["title"], resume=True, integrate=True
    )
    (Path(path) / "config.py").write_text("SETTING = True\n")
    await worktree.finish_integration(path, branch, "main")
    attempts = 0
    verified = []
    real_verify = BoardLoop._verify_goal

    def seed(loop):
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(loop, "_empty_result_signals", lambda _: (True, "end_turn"))
        loop.goal_verify = True

        async def verify(feature, wt, base, reply):
            gap = await real_verify(loop, feature, wt, base, reply)
            verified.append(gap)
            return "Substantive gap remains" if reply_kind == "substantive-gap" else gap

        monkeypatch.setattr(loop, "_verify_goal", verify)

    async def dispatch(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return "## Summary\nUpdated constant"
        context = await worktree.recovery_context(path, branch)
        if attempts == 2:
            assert context["goal_followup_pending"]
            if reply_kind == "fresh-review":
                await worktree.consume_recovery_progress(path, branch)
                raise ProcessStopped()
            if interruption == "restart":
                raise ProcessStopped()
            if interruption == "provider":
                raise worktree.WorktreeError("coder dispatch failed: 429 rate limit exceeded")
        if reply_kind == "missing":
            return "## Summary\nUpdated constant"
        return "## Summary\nNO_TEST_NEEDED: constant configuration adjustment only"

    opened = AsyncMock(return_value=feature["pr_url"])
    store = FakeLoopStore()

    def record_budget(fid, kind, n):
        feature["labels"] = [label for label in feature["labels"] if not label.startswith(f"budget:{kind}:")]
        feature["labels"].append(f"budget:{kind}:{n}")

    monkeypatch.setattr(store, "record_budget", record_budget, raising=False)
    monkeypatch.setattr("project_board.loop.asyncio.sleep", AsyncMock())
    kwargs = dict(
        feature=feature,
        store=store,
        seed=seed,
        dispatch=dispatch,
        open_pr=opened,
        cfg={"coder": "proto", "goal_fix_max": 1},
    )
    if interruption == "restart" or reply_kind == "fresh-review":
        with pytest.raises(ProcessStopped):
            await _drive_with(monkeypatch, **kwargs)
    await _drive_with(monkeypatch, **kwargs)
    assert verified[0] is not None
    if reply_kind == "valid":
        assert verified[-1] is None
        opened.assert_awaited_once()
    else:
        opened.assert_not_awaited()
        assert any(c[0] == "flag_blocked" for c in store.calls)
        if reply_kind in ("missing", "fresh-review"):
            assert len(verified) == 1
        else:
            assert len(verified) == 2


async def test_recovery_journal_without_worktree():
    assert await worktree.recovery_context(None, "feature") == {}
    await worktree.request_requirement_followup(None, "feature")
    await worktree.consume_recovery_progress(None, "feature", goal_followup=True)


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("repair_edits", [False, True])
async def test_clean_base_merge_after_consumed_repair_is_not_coder_progress(
    history, monkeypatch, committed, restart, repair_edits
):
    """A gate consumes the candidate; the base then advances cleanly. The board's
    merge of that base is board output: with or without a restart while the merge
    is pending, an edit-free reply must not mint repair provenance or publish the
    still-failing candidate on gate-budget exhaustion. A genuine edit still counts."""
    from pathlib import Path
    from unittest.mock import AsyncMock
    from tests.test_loop import FEATURE, FakeLoopStore, _drive_with

    class ProcessStopped(BaseException):
        pass

    repo = history
    base = sibling(repo, "a")
    feature = {**FEATURE, "pr_url": "https://example/pr/7", "labels": []}
    branch = worktree.branch_name(feature["id"], feature["title"])
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    remote = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(
        str(repo), "main", feature["id"], title=feature["title"], resume=True, integrate=True
    )
    resolution = "feature coverage\nsibling a coverage\n"
    (Path(path) / "a").write_text(resolution)
    if committed:
        await worktree.finish_integration(path, branch, "main")
    store = FakeLoopStore()

    def record_budget(fid, kind, n):
        feature["labels"] = [label for label in feature["labels"] if not label.startswith(f"budget:{kind}:")]
        feature["labels"].append(f"budget:{kind}:{n}")

    monkeypatch.setattr(store, "record_budget", record_budget, raising=False)
    advanced = []

    def seed(loop):
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(loop, "_empty_result_signals", lambda _: (True, "end_turn"))

    async def check(path, feature):
        if (Path(path) / "gate-fix").exists():
            return None
        if not advanced:
            # The base moves cleanly while the rejected candidate waits for its repair.
            (repo / "c").write_text("clean sibling coverage\n")
            git(repo, "add", "c")
            git(repo, "commit", "-m", "clean sibling c")
            git(repo, "push", "origin", "main")
            advanced.append(git(repo, "rev-parse", "HEAD"))
        return "Missing regression test"

    gate = AsyncMock(side_effect=check)
    attempts = []
    consumed = []

    async def dispatch(coder, resumed_path, prompt, **kwargs):
        assert resumed_path == path
        attempts.append(prompt)
        if len(attempts) == 1:
            return "## Summary\nRetained resolution"
        # The board prepared the clean merge of the advanced base before this reply.
        assert git(path, "rev-parse", "MERGE_HEAD") == advanced[0]
        if len(attempts) == 2:
            assert "Missing regression test" in prompt
            consumed.append(await worktree.recovery_context(path, branch))
            if restart:
                raise ProcessStopped()
        if repair_edits:
            (Path(path) / "gate-fix").write_text("regression coverage\n")
        return "## Summary\nNo further change"

    async def publish(path, branch, **kwargs):
        for ref in (remote, base, advanced[0]):
            git(path, "merge-base", "--is-ancestor", ref, "HEAD")
        assert (Path(path) / "a").read_text() == resolution
        assert (Path(path) / "gate-fix").exists()
        git(path, "push", "origin", branch)
        return feature["pr_url"]

    opened = AsyncMock(side_effect=publish)
    kwargs = dict(
        feature=feature,
        store=store,
        seed=seed,
        dispatch=dispatch,
        open_pr=opened,
        gate=gate,
        cfg={"coder": "proto", "local_gate_max": 1},
    )
    if restart:
        with pytest.raises(ProcessStopped):
            await _drive_with(monkeypatch, **kwargs)
        assert "budget:gate-fix:1" in feature["labels"]
        # A pending board merge alone is not preserved coder progress.
        assert not await worktree.preserved_recovery_progress(path, branch)
    await _drive_with(monkeypatch, **kwargs)
    assert len(attempts) == (3 if restart else 2)
    journal = await worktree.recovery_context(str(repo), branch)
    if repair_edits:
        assert gate.await_count == 2
        opened.assert_awaited_once()
        assert "open_review" in store.names()
    else:
        # Exhausting the gate budget must not publish the still-failing candidate.
        gate.assert_awaited_once()
        opened.assert_not_awaited()
        assert any(c[0] == "flag_blocked" and "fix round produced no commit" in c[2] for c in store.calls)
        assert git(repo, "ls-remote", "origin", f"refs/heads/{branch}").split()[0] == remote
        # No repair provenance was minted for the board's merge.
        assert journal["repair_head_sha"] == consumed[0]["repair_head_sha"]
        assert journal.get("consumed_repair", {}).get("repair_head_sha") == consumed[0]["repair_head_sha"]


@pytest.mark.parametrize(
    ("failure", "detail"),
    [("fetch", "fetch failed"), ("rebase", "Interrupted rebase"), ("rewind", "history was rewritten")],
)
async def test_auto_unblock_refresh_failure_alerts_and_preserves_recovery(history, monkeypatch, failure, detail):
    from pathlib import Path
    from unittest.mock import Mock

    from project_board.loop import BoardLoop
    from tests.test_loop import _BlockedStore, _blocked

    repo = history
    fid, title = "bd-refresh-failure", "Preserve failed recovery"
    branch = worktree.branch_name(fid, title)
    git(repo, "branch", "-m", branch)
    git(repo, "push", "origin", branch)
    git(repo, "checkout", "main")
    path, _ = await worktree.create_worktree(str(repo), "main", fid, title=title, resume=True, integrate=True)
    context = await worktree.recovery_context(path, branch)
    context["published_head_sha"] = git(path, "rev-parse", "HEAD")
    await worktree._record_recovery(path, branch, context)
    remote = git(repo, "remote", "get-url", "origin")
    if failure == "fetch":
        git(repo, "remote", "set-url", "origin", str(repo / "missing"))
    elif failure == "rebase":
        (repo / "a").write_text("sibling coverage\n")
        git(repo, "commit", "-am", "conflicting sibling")
        git(repo, "push", "origin", "main")
        result = subprocess.run(["git", "-C", path, "rebase", "main"], capture_output=True)
        assert result.returncode != 0
    else:
        git(remote, "update-ref", f"refs/heads/{branch}", git(repo, "rev-parse", "main"))
    (Path(path) / "unique").write_text("sole copy of recovery work\n")
    head, status = git(path, "rev-parse", "HEAD"), git(path, "status", "--porcelain")
    blocked = _BlockedStore(
        [
            {**_blocked(fid, "merge-conflict", title=title), "pr_url": "https://example/pr/1"},
            _blocked("bd-other", "transient"),
        ]
    )
    alerts = Mock()
    # A restarted loop must report the same incident, for inbox deduplication.
    for _ in range(2):
        loop = BoardLoop({"coder": "proto"})
        monkeypatch.setattr(loop, "_repo_for", lambda _: str(repo))
        monkeypatch.setattr(loop, "_base_branch_for", lambda _: "main")
        monkeypatch.setattr(loop, "_notify_operator", alerts)
        loop._ci_feedback[fid] = "old conflict feedback"
        await loop._recover_blocked(blocked)
        assert blocked.get_feature(fid)["blocked"]
        assert fid not in blocked.cleared and fid not in blocked.requeued
        assert not any(item[0] == fid for item in blocked.budgets)
        assert loop._ci_feedback[fid] == "old conflict feedback"
        assert git(path, "rev-parse", "HEAD") == head
        assert git(path, "status", "--porcelain") == status
        assert (Path(path) / "unique").read_text() == "sole copy of recovery work\n"
        if failure == "rebase":
            assert await worktree._operation_present(path, "rebase-merge")
    assert blocked.requeued == ["bd-other"]  # failure does not stop the sweep
    assert alerts.call_count == 2
    for call in alerts.call_args_list:
        assert call.args[0] == fid
        assert detail in call.args[1]
        assert call.kwargs["store"] is blocked
    assert alerts.call_args_list[0].kwargs["incident"] == alerts.call_args_list[1].kwargs["incident"]
    if failure == "fetch":
        git(repo, "remote", "set-url", "origin", remote)
        await loop._recover_blocked(blocked)
        assert fid in blocked.cleared and fid in blocked.requeued
        assert (fid, "unblock-retry", 1) in blocked.budgets
        assert "Actual conflict paths: none." in loop._ci_feedback[fid]
