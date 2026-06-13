"""Worktree tests — the PR plumbing (commit → empty-diff guard → push → PR).

``git`` and ``gh`` are shelled out via the module-level ``_git`` / ``_gh`` async
helpers; both are replaced with fakes that return canned ``(rc, stdout, stderr)``
keyed by subcommand, so nothing touches a real repo or GitHub. The contract under
test: the empty-diff guard raises ``NoChangesError`` (the loop escalates that), a
push/`gh` failure raises ``WorktreeError`` (the loop blocks), and an existing PR is
reused on a re-dispatch.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import types

import pytest

from project_board import worktree
from project_board.worktree import CoderTimeout, NoChangesError, WorktreeError


class FakeGit:
    """Stand-in for ``_git(repo, *args, timeout=…)`` — canned per git subcommand."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    async def __call__(self, repo, *args, timeout=60):
        self.calls.append(args)
        return self.responses.get(args[0] if args else "", (0, "", ""))

    def ran(self, sub):
        return [a for a in self.calls if a and a[0] == sub]


class FakeGh:
    """Stand-in for ``_gh(*args, cwd=…, timeout=…)`` — canned per ``gh pr <verb>``."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    async def __call__(self, *args, cwd, timeout=60):
        self.calls.append(args)
        verb = args[1] if len(args) > 1 else ""
        return self.responses.get(verb, (0, "https://example/pr/1", ""))


def _install(monkeypatch, git, gh):
    monkeypatch.setattr(worktree, "_git", git)
    monkeypatch.setattr(worktree, "_gh", gh)


# ── open_pr ─────────────────────────────────────────────────────────────────────


async def test_open_pr_happy_path_pushes_and_returns_url(monkeypatch):
    git = FakeGit({"status": (0, " M f.py", ""), "rev-list": (0, "1", "")})
    gh = FakeGh({"create": (0, "https://example/pr/42", "")})
    _install(monkeypatch, git, gh)
    url = await worktree.open_pr("/wt", "feat/bd-1", base="main", title="feat: x", body="b")
    assert url == "https://example/pr/42"
    assert git.ran("push")  # the branch was pushed
    assert git.ran("commit")  # the dirty tree was committed


async def test_open_pr_raises_no_changes_on_an_empty_diff(monkeypatch):
    git = FakeGit({"status": (0, "", ""), "rev-list": (0, "0", "")})
    _install(monkeypatch, git, FakeGh())
    with pytest.raises(NoChangesError):
        await worktree.open_pr("/wt", "feat/bd-1", base="main", title="t")
    assert not git.ran("push")  # nothing to push → never reaches push


async def test_open_pr_reuses_an_existing_pr_on_redispatch(monkeypatch):
    git = FakeGit({"status": (0, "", ""), "rev-list": (0, "2", "")})
    gh = FakeGh(
        {
            "create": (1, "", "a pull request for branch already exists"),
            "view": (0, "https://example/pr/existing", ""),
        }
    )
    _install(monkeypatch, git, gh)
    url = await worktree.open_pr("/wt", "feat/bd-1", base="main", title="t")
    assert url == "https://example/pr/existing"


async def test_open_pr_blocks_on_a_push_failure(monkeypatch):
    git = FakeGit({"status": (0, "", ""), "rev-list": (0, "1", ""), "push": (1, "", "remote rejected")})
    _install(monkeypatch, git, FakeGh())
    with pytest.raises(WorktreeError, match="push failed"):
        await worktree.open_pr("/wt", "feat/bd-1", base="main", title="t")


# ── commit_worktree ─────────────────────────────────────────────────────────────


async def test_commit_worktree_is_a_noop_on_a_clean_tree(monkeypatch):
    git = FakeGit({"status": (0, "", "")})
    _install(monkeypatch, git, FakeGh())
    await worktree.commit_worktree("/wt", "msg")
    assert not git.ran("add") and not git.ran("commit")


async def test_commit_worktree_stages_and_commits_a_dirty_tree(monkeypatch):
    git = FakeGit({"status": (0, " M f.py", "")})
    _install(monkeypatch, git, FakeGh())
    await worktree.commit_worktree("/wt", "msg")
    assert git.ran("add") and git.ran("commit")


# ── create_worktree: branch off the freshest base ───────────────────────────────


async def test_create_worktree_branches_off_origin_when_it_resolves(monkeypatch):
    git = FakeGit({"rev-parse": (0, "abc123", "")})  # origin/main resolves
    _install(monkeypatch, git, FakeGh())
    path, branch = await worktree.create_worktree("/repo", "main", "bd-1", root=".worktrees")
    assert branch == "feat/bd-1"
    assert path.endswith("/.worktrees/feat-bd-1")
    add = next(a for a in git.calls if a[:2] == ("worktree", "add"))
    assert add[-1] == "origin/main"  # started from the remote tip


async def test_create_worktree_falls_back_to_local_base_without_a_remote(monkeypatch):
    git = FakeGit({"rev-parse": (1, "", "")})  # origin/main does NOT resolve
    _install(monkeypatch, git, FakeGh())
    _path, branch = await worktree.create_worktree("/repo", "main", "bd-2", root=".worktrees")
    assert branch == "feat/bd-2"
    add = next(a for a in git.calls if a[:2] == ("worktree", "add"))
    assert add[-1] == "main"  # fell back to the local branch


# ── pr_is_merged: the merge-poll probe ──────────────────────────────────────────


@pytest.mark.parametrize(
    "gh_state,expected",
    [
        ((0, "MERGED", ""), True),
        ((0, "OPEN", ""), False),
        ((0, "CLOSED", ""), False),  # closed-unmerged is NOT done
        ((1, "", "no pr found"), False),  # a gh failure → False, the poll retries
    ],
)
async def test_pr_is_merged(monkeypatch, gh_state, expected):
    gh = FakeGh({"view": gh_state})
    _install(monkeypatch, FakeGit(), gh)
    assert await worktree.pr_is_merged("https://example/pr/1", cwd="/repo") is expected


# ── pr_url_for_branch: the crash-recovery probe ─────────────────────────────────


async def test_pr_url_for_branch_found_and_absent(monkeypatch):
    _install(monkeypatch, FakeGit(), FakeGh({"view": (0, "https://example/pr/9", "")}))
    assert await worktree.pr_url_for_branch("feat/bd-9", cwd="/repo") == "https://example/pr/9"
    _install(monkeypatch, FakeGit(), FakeGh({"view": (1, "", "no pull requests found")}))
    assert await worktree.pr_url_for_branch("feat/bd-9", cwd="/repo") == ""


# ── reap_feature_worktree: the shared id → worktree/branch reap ──────────────────


async def test_reap_feature_worktree_computes_path_and_branch(monkeypatch):
    calls = []

    async def _remove(repo, wt, branch=""):
        calls.append((repo, wt, branch))

    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    await worktree.reap_feature_worktree("/repo", ".worktrees", "bd-7")
    assert calls == [("/repo", "/repo/.worktrees/feat-bd-7", "feat/bd-7")]


# ── dispatch_coder: the stuck-coder watchdog (hard timeout + reap) ──────────────


def _inject_fake_delegates(monkeypatch, acp):
    """Stand in for the host's `plugins.delegates.adapters` (absent in the suite),
    which dispatch_coder imports lazily."""

    class _DelegateError(Exception):
        pass

    mod = types.ModuleType("plugins.delegates.adapters")
    mod.ADAPTERS = {"acp": acp}
    mod.DelegateError = _DelegateError
    pkg = types.ModuleType("plugins")
    sub = types.ModuleType("plugins.delegates")
    pkg.delegates = sub
    sub.adapters = mod
    monkeypatch.setitem(sys.modules, "plugins", pkg)
    monkeypatch.setitem(sys.modules, "plugins.delegates", sub)
    monkeypatch.setitem(sys.modules, "plugins.delegates.adapters", mod)
    return _DelegateError


@dataclasses.dataclass
class _Coder:
    workdir: str = ""


async def test_dispatch_coder_raises_coder_timeout_and_reaps_on_overrun(monkeypatch):
    teardowns = []

    class _Acp:
        async def dispatch(self, scoped, prompt, timeout=None):
            await asyncio.sleep(3600)  # hang well past the timeout

        async def teardown(self, scoped):
            teardowns.append(scoped)

    _inject_fake_delegates(monkeypatch, _Acp())
    with pytest.raises(CoderTimeout):
        await worktree.dispatch_coder(_Coder(), "/wt", "do it", timeout=0.01)
    assert teardowns  # the hung subprocess is still reaped (the finally ran)


async def test_dispatch_coder_returns_result_within_budget(monkeypatch):
    class _Acp:
        async def dispatch(self, scoped, prompt, timeout=None):
            return "built it"

        async def teardown(self, scoped):
            pass

    _inject_fake_delegates(monkeypatch, _Acp())
    out = await worktree.dispatch_coder(_Coder(), "/wt", "do it", timeout=5)
    assert out == "built it"
