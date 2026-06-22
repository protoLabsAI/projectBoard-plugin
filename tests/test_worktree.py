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


# ── stage_all: keep the coder's own scratch out of the PR (#49) ──────────────────


async def test_stage_all_excludes_coder_scratch(monkeypatch):
    git = FakeGit()
    monkeypatch.setattr(worktree, "_git", git)
    await worktree.stage_all("/wt")
    (add,) = git.ran("add")
    # `add -A` over a positive `.` with an exclude pathspec per scratch path — so the
    # coder's `.proto/` session notes + `.cursor` cache never get staged into the commit.
    assert add[:4] == ("add", "-A", "--", ".")
    excludes = set(add[4:])
    assert excludes == {f":(exclude){p}" for p in worktree.CODER_SCRATCH}
    assert ":(exclude).proto" in excludes


async def test_commit_worktree_stages_without_the_scratch(monkeypatch):
    """The commit path stages via stage_all, so the dirty-tree commit excludes scratch."""
    git = FakeGit({"status": (0, " M f.py\n?? .proto/", "")})
    _install(monkeypatch, git, FakeGh())
    await worktree.commit_worktree("/wt", "msg")
    (add,) = git.ran("add")
    assert ":(exclude).proto" in add and git.ran("commit")


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


# ── promote_worktree: the Max-Mode winner takes the canonical name (#21) ─────────


async def test_promote_worktree_moves_dir_and_renames_branch(monkeypatch):
    git = FakeGit()
    monkeypatch.setattr(worktree, "_git", git)
    canon_wt, canon_branch = await worktree.promote_worktree(
        "/repo", "/repo/.worktrees/feat-bd-1.c2", "feat/bd-1.c2", "bd-1", root=".worktrees"
    )
    assert canon_branch == "feat/bd-1"
    assert canon_wt.endswith("/.worktrees/feat-bd-1")
    move = next(a for a in git.calls if a[:2] == ("worktree", "move"))
    assert move[2].endswith("/.worktrees/feat-bd-1.c2") and move[3].endswith("/.worktrees/feat-bd-1")
    rename = next(a for a in git.calls if a[:1] == ("branch",) and "-m" in a)
    assert rename == ("branch", "-m", "feat/bd-1.c2", "feat/bd-1")


async def test_promote_worktree_is_a_noop_when_already_canonical(monkeypatch):
    git = FakeGit()
    monkeypatch.setattr(worktree, "_git", git)
    _wt, branch = await worktree.promote_worktree(
        "/repo", "/repo/.worktrees/feat-bd-1", "feat/bd-1", "bd-1", root=".worktrees"
    )
    assert branch == "feat/bd-1"
    assert not git.ran("move") and not [a for a in git.calls if a[:1] == ("worktree",)]


# ── pr_is_merged: the merge-poll probe ──────────────────────────────────────────


@pytest.mark.parametrize(
    "gh_state,expected",
    [
        ((0, "MERGED", ""), "MERGED"),
        ((0, "OPEN", ""), "OPEN"),
        ((0, "CLOSED", ""), "CLOSED"),
        ((1, "", "no pr found"), ""),  # a gh failure → "", the reconcile retries
    ],
)
async def test_pr_state(monkeypatch, gh_state, expected):
    gh = FakeGh({"view": gh_state})
    _install(monkeypatch, FakeGit(), gh)
    assert await worktree.pr_state("https://example/pr/1", cwd="/repo") == expected


# ── pr_url_for_branch: the crash-recovery probe ─────────────────────────────────


async def test_pr_url_for_branch_found_and_absent(monkeypatch):
    _install(monkeypatch, FakeGit(), FakeGh({"view": (0, "https://example/pr/9", "")}))
    assert await worktree.pr_url_for_branch("feat/bd-9", cwd="/repo") == "https://example/pr/9"
    _install(monkeypatch, FakeGit(), FakeGh({"view": (1, "", "no pull requests found")}))
    assert await worktree.pr_url_for_branch("feat/bd-9", cwd="/repo") == ""


# ── list_feature_worktrees: the health sweep's orphan enumeration ───────────────


def test_list_feature_worktrees(tmp_path):
    root = tmp_path / "wt"
    (root / "feat-bd-1").mkdir(parents=True)
    (root / "feat-bd-2").mkdir()
    (root / "other-dir").mkdir()  # not a feat- dir
    (root / "feat-stray-file").write_text("x")  # a file, not a worktree dir
    assert set(worktree.list_feature_worktrees(str(tmp_path), "wt")) == {"bd-1", "bd-2"}


def test_list_feature_worktrees_absent_dir(tmp_path):
    assert worktree.list_feature_worktrees(str(tmp_path), "does-not-exist") == []


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


# ── pr_ci_status: the closed-loop verify rollup ─────────────────────────────────


def _ci_gh(rollup, log_out=""):
    """A `_gh` stub for pr_ci_status: returns the statusCheckRollup JSON for the
    `pr view` call, and a failing-log for the follow-up `run view --log-failed`."""

    async def _gh(*args, cwd, timeout=60):
        if "statusCheckRollup" in args:
            return (0, rollup, "")
        if args and args[0] == "run":
            return (0, log_out, "")
        return (0, "", "")

    return _gh


async def test_pr_ci_status_passing(monkeypatch):
    rollup = '[{"name":"Lint","conclusion":"SUCCESS"},{"name":"Tests","conclusion":"SUCCESS"}]'
    monkeypatch.setattr(worktree, "_gh", _ci_gh(rollup))
    status, summary = await worktree.pr_ci_status("https://example/pr/1", cwd="/repo")
    assert status == "passing" and summary == ""


async def test_pr_ci_status_pending(monkeypatch):
    rollup = '[{"name":"Tests","status":"IN_PROGRESS"},{"name":"Lint","conclusion":"SUCCESS"}]'
    monkeypatch.setattr(worktree, "_gh", _ci_gh(rollup))
    status, _ = await worktree.pr_ci_status("https://example/pr/1", cwd="/repo")
    assert status == "pending"


async def test_pr_ci_status_failing_includes_name_and_log(monkeypatch):
    rollup = (
        '[{"name":"Web E2E","conclusion":"FAILURE",'
        '"detailsUrl":"https://github.com/o/r/actions/runs/123/job/456"},'
        '{"name":"Lint","conclusion":"SUCCESS"}]'
    )
    monkeypatch.setattr(worktree, "_gh", _ci_gh(rollup, log_out="settings.spec.ts:71 element(s) not found"))
    status, summary = await worktree.pr_ci_status("https://example/pr/1", cwd="/repo")
    assert status == "failing"
    assert "Web E2E: FAILURE" in summary
    assert "element(s) not found" in summary  # the failing-run log got pulled in


async def test_pr_ci_status_none_when_no_checks_or_gh_error(monkeypatch):
    monkeypatch.setattr(worktree, "_gh", _ci_gh("[]"))
    assert await worktree.pr_ci_status("https://example/pr/1", cwd="/repo") == ("none", "")

    async def _err(*args, cwd, timeout=60):
        return (1, "", "no pr")

    monkeypatch.setattr(worktree, "_gh", _err)
    assert await worktree.pr_ci_status("https://example/pr/1", cwd="/repo") == ("none", "")


async def test_dispatch_coder_forgets_session_before_dispatch(monkeypatch):
    """Fresh-both: the worktree is recreated per attempt, so dispatch_coder must
    forget the ACP session BEFORE dispatching (else a resumed thread's memory would
    reference a wiped diff). Assert the order: forget → dispatch → teardown."""
    order = []

    class _Acp:
        async def forget_session(self, scoped):
            order.append("forget")
            return True

        async def dispatch(self, scoped, prompt, *, timeout=None):
            order.append("dispatch")
            return "built it"

        async def teardown(self, scoped):
            order.append("teardown")

    _inject_fake_delegates(monkeypatch, _Acp())
    out = await worktree.dispatch_coder(_Coder(), "/wt", "do it", timeout=5)
    assert out == "built it"
    assert order == ["forget", "dispatch", "teardown"]


# ── link_node_modules: share the main repo's deps with the worktree ────────────────


def test_link_node_modules_symlinks_root_and_monorepo_packages(tmp_path):
    repo = tmp_path / "repo"
    (repo / "node_modules" / "react").mkdir(parents=True)  # root deps
    (repo / "packages" / "ui" / "node_modules" / "vite").mkdir(parents=True)  # workspace deps
    (repo / "src").mkdir()
    wt = tmp_path / "wt"
    (wt / "packages" / "ui").mkdir(parents=True)  # the worktree checkout has the source tree
    n = worktree.link_node_modules(str(repo), str(wt))
    assert n == 2
    assert (wt / "node_modules").is_symlink()
    assert (wt / "node_modules" / "react").is_dir()  # resolves through the symlink
    assert (wt / "packages" / "ui" / "node_modules").is_symlink()
    assert (wt / "packages" / "ui" / "node_modules" / "vite").is_dir()


def test_link_node_modules_noop_for_a_non_node_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)  # no node_modules
    wt = tmp_path / "wt"
    wt.mkdir()
    assert worktree.link_node_modules(str(repo), str(wt)) == 0
    assert not (wt / "node_modules").exists()


def test_link_node_modules_does_not_descend_into_node_modules(tmp_path):
    """A nested node_modules/<pkg>/node_modules must NOT be separately linked (pruned)."""
    repo = tmp_path / "repo"
    (repo / "node_modules" / "a" / "node_modules" / "b").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    assert worktree.link_node_modules(str(repo), str(wt)) == 1  # only the top-level one
