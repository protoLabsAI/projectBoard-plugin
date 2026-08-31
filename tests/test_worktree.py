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
import logging
import os
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


def _adopt_gh(*, view_json, ready=(0, "", "")):
    """A `_gh` stub for the open_pr reuse path (#207): `pr create` says the PR exists,
    `pr view --json url` resolves it, `pr view --json isDraft,mergeStateStatus` returns
    ``view_json``, and `pr ready` answers ``ready``. Records every call."""
    calls = []

    async def _gh(*args, cwd, timeout=60):
        calls.append(args)
        verb = args[1] if len(args) > 1 else ""
        if verb == "create":
            return (1, "", "a pull request for branch already exists")
        if verb == "view" and "isDraft,mergeStateStatus" in args:
            return view_json
        if verb == "view":
            return (0, "https://example/pr/existing", "")
        if verb == "ready":
            return ready
        return (0, "", "")

    _gh.calls = calls
    return _gh


async def test_open_pr_adopting_a_coder_made_draft_marks_it_ready(monkeypatch, caplog):
    """#207: the coder ran `gh pr create --draft` itself; the loop's `pr create` hits
    "already exists" and adopts the URL. A draft must not ride into the review/merge
    gates (GitHub says CLEAN for a draft, `gh pr merge` refuses it) — the loop owns
    the PR lifecycle, so it marks the PR ready right here and says so."""
    git = FakeGit({"status": (0, "", ""), "rev-list": (0, "2", "")})
    gh = _adopt_gh(view_json=(0, '{"isDraft": true, "mergeStateStatus": "CLEAN"}', ""))
    _install(monkeypatch, git, gh)
    with caplog.at_level("INFO", logger="protoagent.plugins.project_board"):
        url = await worktree.open_pr("/wt", "feat/bd-1", base="main", title="t")
    assert url == "https://example/pr/existing"
    assert ("pr", "ready", "https://example/pr/existing") in gh.calls
    assert "adopted the coder's DRAFT PR https://example/pr/existing — marked ready" in caplog.text
    assert "the loop owns the PR lifecycle" in caplog.text


async def test_open_pr_leaves_a_draft_alone_when_promote_draft_is_off(monkeypatch):
    """The re-dispatch of a card that already owns its PR (#207 review): the loop
    passes promote_draft=False, so an operator's draft-as-hold on the loop's own PR
    survives the CI-fail bounce — the URL is still adopted, no isDraft read, no
    `gh pr ready`."""
    git = FakeGit({"status": (0, "", ""), "rev-list": (0, "2", "")})
    gh = _adopt_gh(view_json=(0, '{"isDraft": true, "mergeStateStatus": "CLEAN"}', ""))
    _install(monkeypatch, git, gh)
    url = await worktree.open_pr("/wt", "feat/bd-1", base="main", title="t", promote_draft=False)
    assert url == "https://example/pr/existing"
    assert not [c for c in gh.calls if c[1] == "ready" or "isDraft,mergeStateStatus" in c]


async def test_open_pr_adopting_a_non_draft_never_runs_pr_ready(monkeypatch):
    """The loop's OWN PR on a re-dispatch (not a draft) — byte-for-byte the old path."""
    git = FakeGit({"status": (0, "", ""), "rev-list": (0, "2", "")})
    gh = _adopt_gh(view_json=(0, '{"isDraft": false, "mergeStateStatus": "BEHIND"}', ""))
    _install(monkeypatch, git, gh)
    url = await worktree.open_pr("/wt", "feat/bd-1", base="main", title="t")
    assert url == "https://example/pr/existing"
    assert not [c for c in gh.calls if c[1] == "ready"]


async def test_open_pr_draft_promotion_is_best_effort(monkeypatch, caplog):
    """`gh pr ready` failing (or the isDraft read failing) logs a warning and still
    returns the PR URL — the auto-merge edge's named `draft` blocker is the backstop."""
    git = FakeGit({"status": (0, "", ""), "rev-list": (0, "2", "")})
    gh = _adopt_gh(
        view_json=(0, '{"isDraft": true, "mergeStateStatus": "CLEAN"}', ""), ready=(1, "", "GraphQL: forbidden")
    )
    _install(monkeypatch, git, gh)
    with caplog.at_level("WARNING", logger="protoagent.plugins.project_board"):
        url = await worktree.open_pr("/wt", "feat/bd-1", base="main", title="t")
    assert url == "https://example/pr/existing"
    assert "`gh pr ready` failed (GraphQL: forbidden)" in caplog.text
    # the isDraft read itself failing → no `pr ready`, URL still returned
    gh = _adopt_gh(view_json=(1, "", "boom"))
    _install(monkeypatch, git, gh)
    assert await worktree.open_pr("/wt", "feat/bd-1", base="main", title="t") == "https://example/pr/existing"
    assert not [c for c in gh.calls if c[1] == "ready"]


async def test_pr_merge_info_reads_status_and_draft_in_one_call(monkeypatch):
    calls = []

    async def _gh(*args, cwd, timeout=60):
        calls.append(args)
        return (0, '{"isDraft": true, "mergeStateStatus": "CLEAN"}', "")

    monkeypatch.setattr(worktree, "_gh", _gh)
    assert await worktree.pr_merge_info("https://x/pr/1") == {"mergeStateStatus": "CLEAN", "isDraft": True}
    assert await worktree.pr_merge_state("https://x/pr/1") == "CLEAN"  # the status half, same read
    assert all("isDraft,mergeStateStatus" in c for c in calls) and len(calls) == 2

    async def _bad(*args, cwd, timeout=60):
        return (1, "", "not found")

    monkeypatch.setattr(worktree, "_gh", _bad)
    assert await worktree.pr_merge_info("https://x/pr/1") == {"mergeStateStatus": "", "isDraft": None}
    assert await worktree.pr_merge_state("https://x/pr/1") == ""

    async def _garbage(*args, cwd, timeout=60):
        return (0, "not json", "")

    monkeypatch.setattr(worktree, "_gh", _garbage)
    assert await worktree.pr_merge_info("https://x/pr/1") == {"mergeStateStatus": "", "isDraft": None}


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


# ── #227: human-readable branch names — feat/<fid>-<slug> ────────────────────────


def test_slugify_lowercases_collapses_strips_and_truncates():
    assert worktree.slugify("Hello,   World!!") == "hello-world"  # collapse the punctuation run
    assert worktree.slugify("  --Leading/Trailing.-- ") == "leading-trailing"  # strip both ends
    assert worktree.slugify("") == "" and worktree.slugify("!!!") == ""  # empty / all-punct → ""
    assert worktree.slugify("x" * 100) == "x" * 40  # hard cap at max_len
    # a cut that lands right after a hyphen strips the dangling hyphen
    assert worktree.slugify("a" * 39 + " boom") == "a" * 39


def test_branch_and_dir_carry_the_slugged_title_truncated_to_40():
    """The acceptance example: fid `bd-t7bk.1`, title `Add reusable plugin-release.yml
    workflow` → the 40-char slug rides both the branch and the worktree dir."""
    fid, title = "bd-t7bk.1", "Add reusable plugin-release.yml workflow"
    assert worktree.slugify(title) == "add-reusable-plugin-release-yml-workflow"
    assert len(worktree.slugify(title)) == 40
    assert worktree.branch_name(fid, title) == "feat/bd-t7bk.1-add-reusable-plugin-release-yml-workflow"
    assert worktree.worktree_dir(fid, title) == "feat-bd-t7bk.1-add-reusable-plugin-release-yml-workflow"


def test_branch_and_dir_fall_back_to_the_bare_fid_without_a_slug():
    """An empty/all-punctuation title (or none at all — the candidate case) keeps the
    machine-key-only `feat-<fid>` / `feat/<fid>` shape the pre-#227 code produced."""
    assert worktree.branch_name("bd-1", "") == "feat/bd-1"
    assert worktree.branch_name("bd-1", "!!!") == "feat/bd-1"
    assert worktree.worktree_dir("bd-1") == "feat-bd-1"  # title defaults to ""


async def test_create_worktree_slugs_the_branch_and_dir_from_the_title(monkeypatch):
    git = FakeGit({"rev-parse": (0, "abc123", "")})
    _install(monkeypatch, git, FakeGh())
    path, branch = await worktree.create_worktree(
        "/repo", "main", "bd-t7bk.1", root=".worktrees", title="Add reusable plugin-release.yml workflow"
    )
    assert branch == "feat/bd-t7bk.1-add-reusable-plugin-release-yml-workflow"
    assert path.endswith("/.worktrees/feat-bd-t7bk.1-add-reusable-plugin-release-yml-workflow")
    add = next(a for a in git.calls if a[:2] == ("worktree", "add"))
    assert add[2].endswith("feat-bd-t7bk.1-add-reusable-plugin-release-yml-workflow")  # the rel path
    assert add[4] == branch  # ... -b <slugged-branch> ...


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


async def test_resume_starts_a_fix_round_from_the_pr_head_not_from_base(monkeypatch):
    """A fix round must open the coder on the branch it is being asked to correct.

    Without this the coder gets a clean tree off base while the review prompt tells it to
    "fix every finding in the existing branch" — the prompt and the filesystem disagree,
    so it has to re-implement the whole change before it can touch a one-line finding. On
    a large card that never fits the dispatch timeout: three 30-minute rounds, zero
    commits, observed live on PR #332."""
    git = FakeGit({"rev-parse": (0, "abc123", "")})  # both origin/main and the branch resolve
    _install(monkeypatch, git, FakeGh())
    _path, branch = await worktree.create_worktree("/repo", "main", "bd-1", root=".worktrees", resume=True)
    add = next(a for a in git.calls if a[:2] == ("worktree", "add"))
    assert add[-1] == f"origin/{branch}"  # the PR head, NOT origin/main
    assert ("fetch", "origin", branch) in git.calls  # and it was fetched first


async def test_resume_falls_back_to_base_when_the_branch_is_gone(monkeypatch):
    """A card whose remote branch was deleted (a squash-merge cleanup, a manual tidy)
    must still BUILD rather than fail its dispatch — resume is an optimisation, never a
    precondition."""
    git = FakeGit({"rev-parse": (1, "", "")})  # nothing resolves
    _install(monkeypatch, git, FakeGh())
    _path, _branch = await worktree.create_worktree("/repo", "main", "bd-2", root=".worktrees", resume=True)
    add = next(a for a in git.calls if a[:2] == ("worktree", "add"))
    assert add[-1] == "main"


async def test_a_fresh_build_never_resumes(monkeypatch):
    """The default is unchanged: a card with no PR starts off base, one throwaway tree."""
    git = FakeGit({"rev-parse": (0, "abc123", "")})
    _install(monkeypatch, git, FakeGh())
    _path, branch = await worktree.create_worktree("/repo", "main", "bd-3", root=".worktrees")
    add = next(a for a in git.calls if a[:2] == ("worktree", "add"))
    assert add[-1] == "origin/main"
    assert ("fetch", "origin", branch) not in git.calls


# ── #225: prune stale worktrees so create_worktree survives a corrupt admin dir ──


class _SeqGit:
    """A `_git` fake that keys a canned response on the full SUBCOMMAND (``("worktree",
    <verb>)`` for a worktree op, else ``args[0]``) and can hand out a SEQUENCE of
    responses for one key — so a `worktree add` can fail on the first call and succeed
    on the retry. Records every call."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}
        self._idx = {}

    def _key(self, args):
        if args and args[0] == "worktree":
            return ("worktree", args[1]) if len(args) > 1 else ("worktree",)
        return args[0] if args else ""

    async def __call__(self, repo, *args, timeout=60):
        self.calls.append(args)
        key = self._key(args)
        resp = self.responses.get(key, (0, "", ""))
        if isinstance(resp, list):
            i = min(self._idx.get(key, 0), len(resp) - 1)
            self._idx[key] = i + 1
            return resp[i]
        return resp

    def ran(self, *sub):
        return [a for a in self.calls if a[: len(sub)] == sub]


async def test_create_worktree_prunes_stale_worktrees_before_add(monkeypatch):
    """r2: `git worktree prune` runs BEFORE `worktree add` as a preventive measure."""
    git = _SeqGit({"rev-parse": (0, "abc", "")})
    _install(monkeypatch, git, FakeGh())
    await worktree.create_worktree("/repo", "main", "bd-1", root=".worktrees")
    wt_ops = [a[:2] for a in git.calls if a and a[0] == "worktree"]
    assert ("worktree", "prune") in wt_ops
    assert ("worktree", "add") in wt_ops
    assert wt_ops.index(("worktree", "prune")) < wt_ops.index(("worktree", "add"))
    # the prune is verbose so pruned entries can be surfaced/logged
    assert git.ran("worktree", "prune")[0] == ("worktree", "prune", "-v")


async def test_create_worktree_logs_pruned_stale_worktrees_at_warning(monkeypatch, caplog):
    """r4: when the preventive prune actually cleans a stale worktree (non-empty
    output), it is logged at WARNING."""
    git = _SeqGit(
        {
            "rev-parse": (0, "abc", ""),
            ("worktree", "prune"): (
                0,
                "Removing worktrees/no-fabrication: gitdir file points to non-existent location\n",
                "",
            ),
        }
    )
    _install(monkeypatch, git, FakeGh())
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        _path, branch = await worktree.create_worktree("/repo", "main", "bd-1", root=".worktrees")
    assert branch == "feat/bd-1"
    assert "pruned stale worktree" in caplog.text
    assert "no-fabrication" in caplog.text


async def test_create_worktree_does_not_warn_when_prune_is_clean(monkeypatch, caplog):
    """r6: a healthy repo (prune finds nothing) creates the worktree with NO stale-
    cleanup warning — other projects are unaffected by the new prune step."""
    git = _SeqGit({"rev-parse": (0, "abc", "")})  # prune → default (0, "", "") empty
    _install(monkeypatch, git, FakeGh())
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        await worktree.create_worktree("/repo", "main", "bd-1", root=".worktrees")
    assert "pruned stale worktree" not in caplog.text
    assert len(git.ran("worktree", "add")) == 1  # first add succeeded → no retry


async def test_create_worktree_retries_add_once_after_prune_on_git_error(monkeypatch, caplog):
    """r1/r3: a `worktree add` that fails with 'fatal: not a git repository' (the
    release-tools corruption) triggers a prune + a single retry; the retry succeeds so
    create_worktree returns the path/branch normally."""
    git = _SeqGit(
        {
            "rev-parse": (0, "abc", ""),
            ("worktree", "add"): [
                (128, "", "fatal: not a git repository (or any parent up to mount point /)"),
                (0, "", ""),
            ],
        }
    )
    _install(monkeypatch, git, FakeGh())
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        path, branch = await worktree.create_worktree("/repo", "main", "bd-1", root=".worktrees")
    assert branch == "feat/bd-1"
    assert path.endswith("/.worktrees/feat-bd-1")
    assert len(git.ran("worktree", "add")) == 2  # failed once, retried once
    assert len(git.ran("worktree", "prune")) == 2  # preventive prune + the retry prune
    assert "retrying once" in caplog.text


async def test_create_worktree_raises_when_the_retry_also_fails(monkeypatch):
    """r3: the retry is a SINGLE attempt — if the add still fails after the second
    prune, the WorktreeError propagates (the loop blocks) rather than looping."""
    git = _SeqGit(
        {
            "rev-parse": (0, "abc", ""),
            ("worktree", "add"): (128, "", "fatal: not a git repository"),
        }
    )
    _install(monkeypatch, git, FakeGh())
    with pytest.raises(WorktreeError, match="worktree add failed"):
        await worktree.create_worktree("/repo", "main", "bd-1", root=".worktrees")
    assert len(git.ran("worktree", "add")) == 2  # original + exactly one retry, then give up


async def test_prune_stale_worktrees_is_best_effort_on_nonzero(monkeypatch):
    """A non-zero prune (rc≠0) is swallowed — the return is its stripped stdout and no
    exception rides into the loop; the subsequent `worktree add` reports any real error."""
    git = _SeqGit({("worktree", "prune"): (1, "", "fatal: something")})
    monkeypatch.setattr(worktree, "_git", git)
    assert await worktree.prune_stale_worktrees("/repo") == ""


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


async def test_promote_worktree_slugs_the_canonical_from_the_title(monkeypatch):
    """#227: the winner is promoted to the SLUGGED canonical name so it matches what the
    loop later recovers/reaps by `branch_name(fid, title)`."""
    git = FakeGit()
    monkeypatch.setattr(worktree, "_git", git)
    canon_wt, canon_branch = await worktree.promote_worktree(
        "/repo", "/repo/.worktrees/feat-bd-1.c2", "feat/bd-1.c2", "bd-1", root=".worktrees", title="Add a thing"
    )
    assert canon_branch == "feat/bd-1-add-a-thing"
    assert canon_wt.endswith("/.worktrees/feat-bd-1-add-a-thing")
    rename = next(a for a in git.calls if a[:1] == ("branch",) and "-m" in a)
    assert rename == ("branch", "-m", "feat/bd-1.c2", "feat/bd-1-add-a-thing")


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


def test_list_feature_worktrees_strips_the_slug_tail(tmp_path):
    """#227: a slugged canonical dir resolves to the bare fid (so the sweep looks the
    feature up by its machine key); a candidate keeps its `.g<n>`/`.c<k>` suffix so
    `parent_feature_id` can still resolve its owner."""
    root = tmp_path / "wt"
    (root / "feat-bd-t7bk.1-add-reusable-plugin-release-yml-workflow").mkdir(parents=True)
    (root / "feat-bd-9").mkdir()  # bare (empty-slug) canonical — unchanged
    (root / "feat-bd-9.c1").mkdir()  # a candidate — never slugged
    assert set(worktree.list_feature_worktrees(str(tmp_path), "wt")) == {"bd-t7bk.1", "bd-9", "bd-9.c1"}


def test_wt_id_from_dirname_extracts_the_fid_before_the_slug():
    assert worktree._wt_id_from_dirname("feat-bd-t7bk.1-add-reusable-thing") == "bd-t7bk.1"
    assert worktree._wt_id_from_dirname("feat-bd-9") == "bd-9"  # no slug
    assert worktree._wt_id_from_dirname("feat-bd-9.c1") == "bd-9.c1"  # candidate suffix kept
    assert worktree._wt_id_from_dirname("feat-bd-9.g2-a-slug") == "bd-9.g2"  # defensive: suffix then slug
    assert worktree._wt_id_from_dirname("feat-legacy") == "legacy"  # non-bd id → whole remainder


# ── parent_feature_id: candidate worktrees resolve to their owning feature (#91) ──


def test_parent_feature_id_strips_candidate_suffixes():
    assert worktree.parent_feature_id("bd-1cp.g1") == "bd-1cp"  # coder.solve candidate
    assert worktree.parent_feature_id("bd-1.c2") == "bd-1"  # Max-Mode candidate
    assert worktree.parent_feature_id("bd-1.test") == "bd-1"  # test-rung diagnostic
    assert worktree.parent_feature_id("bd-1.test.g2") == "bd-1"  # stacked: test-rung's own candidate
    assert worktree.parent_feature_id("bd-1cp.g12") == "bd-1cp"  # multi-digit gen


def test_parent_feature_id_leaves_canonical_ids_alone():
    assert worktree.parent_feature_id("bd-13w") == "bd-13w"
    assert worktree.parent_feature_id("bd-1cp") == "bd-1cp"
    # A dot that is NOT a candidate suffix is not a candidate marker — untouched.
    assert worktree.parent_feature_id("bd-1.gx") == "bd-1.gx"


# ── reap_feature_worktree: the shared id → worktree/branch reap ──────────────────


async def test_reap_feature_worktree_computes_path_and_branch(monkeypatch):
    calls = []

    async def _remove(repo, wt, branch=""):
        calls.append((repo, wt, branch))

    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    await worktree.reap_feature_worktree("/repo", ".worktrees", "bd-7")
    assert calls == [("/repo", "/repo/.worktrees/feat-bd-7", "feat/bd-7")]


async def test_reap_finds_a_slugged_canonical_and_derives_its_branch(monkeypatch, tmp_path):
    """#227: the canonical worktree carries a `-<slug>` tail whose slug isn't
    recomputable from the fid alone. reap discovers `feat-<fid>-<slug>` by scan and
    removes it with the matching `feat/<fid>-<slug>` branch. The bare-name attempt still
    fires first (an idempotent no-op)."""
    (tmp_path / ".worktrees" / "feat-bd-7-add-a-thing").mkdir(parents=True)
    calls = []

    async def _remove(repo, wt, branch=""):
        calls.append((os.path.basename(wt), branch))
        return True

    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    await worktree.reap_feature_worktree(str(tmp_path), ".worktrees", "bd-7")
    assert calls[0] == ("feat-bd-7", "feat/bd-7")  # the bare attempt, unchanged
    assert ("feat-bd-7-add-a-thing", "feat/bd-7-add-a-thing") in calls  # the slugged tree + branch


async def test_reap_slug_scan_skips_siblings_and_candidates(monkeypatch, tmp_path):
    """The `-<slug>` scan (a hyphen) must not grab a prefix-sibling (`feat-bd-70-…`), and
    a candidate (`.` separator) is left to the candidate sweep — never the slug scan."""
    root = tmp_path / ".worktrees"
    for name in ("feat-bd-7-my-slug", "feat-bd-70-other-feature", "feat-bd-7.c1"):
        (root / name).mkdir(parents=True)
    reaped = []

    async def _remove(repo, wt, branch=""):
        reaped.append(os.path.basename(wt))
        return True

    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    await worktree.reap_feature_worktree(str(tmp_path), ".worktrees", "bd-7")
    assert "feat-bd-7-my-slug" in reaped  # bd-7's slugged canonical
    assert "feat-bd-7.c1" in reaped  # bd-7's candidate (via the candidate sweep)
    assert "feat-bd-70-other-feature" not in reaped  # the sibling feature is untouched


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


# ── advisory-status filtering: only required checks / GH Actions gate (bd-1zp) ────


def test_is_blocking_check_required_and_actions_block_advisory_does_not():
    # A required check (branch protection) is blocking whatever its provider/type.
    assert worktree._is_blocking_check({"__typename": "StatusContext", "isRequired": True})
    # A GitHub Actions run (a CheckRun) is blocking.
    assert worktree._is_blocking_check({"__typename": "CheckRun", "name": "Tests"})
    # A third-party advisory status (a non-required StatusContext) is NOT.
    assert not worktree._is_blocking_check({"__typename": "StatusContext", "context": "coderabbit"})
    # Unknown shape (older gh with no __typename) is conservatively blocking.
    assert worktree._is_blocking_check({"name": "Legacy", "conclusion": "FAILURE"})


async def test_pr_ci_status_required_check_failure_is_failing(monkeypatch):
    """A failing REQUIRED check is blocking → the rollup reads failing so the feature
    is bounced, even when it arrives as a legacy StatusContext (Test A)."""
    rollup = (
        '[{"__typename":"StatusContext","context":"required-gate","state":"FAILURE","isRequired":true},'
        '{"__typename":"CheckRun","name":"Lint","conclusion":"SUCCESS","workflowName":"CI"}]'
    )
    monkeypatch.setattr(worktree, "_gh", _ci_gh(rollup))
    status, summary = await worktree.pr_ci_status("https://example/pr/1", cwd="/repo")
    assert status == "failing"
    assert "required-gate: FAILURE" in summary


async def test_pr_ci_status_github_action_failure_is_failing(monkeypatch):
    """A failing GitHub Actions run (a CheckRun) is blocking → failing (Test A sibling)."""
    rollup = (
        '[{"__typename":"CheckRun","name":"Web E2E","conclusion":"FAILURE","workflowName":"CI",'
        '"detailsUrl":"https://github.com/o/r/actions/runs/123/job/456"}]'
    )
    monkeypatch.setattr(worktree, "_gh", _ci_gh(rollup, log_out="settings.spec.ts:71 element(s) not found"))
    status, summary = await worktree.pr_ci_status("https://example/pr/1", cwd="/repo")
    assert status == "failing"
    assert "Web E2E: FAILURE" in summary
    assert "element(s) not found" in summary


async def test_pr_ci_status_advisory_failure_is_ignored(monkeypatch):
    """A red third-party ADVISORY status (CodeRabbit / a coverage bot — a non-required
    StatusContext) must NOT read as failing while every blocking check is green, so it
    never bounces the feature (Test B, bd-1zp)."""
    rollup = (
        '[{"__typename":"CheckRun","name":"Tests","conclusion":"SUCCESS","workflowName":"CI"},'
        '{"__typename":"StatusContext","context":"coderabbit","state":"FAILURE"},'
        '{"__typename":"StatusContext","context":"coverage/coveralls","state":"FAILURE"}]'
    )
    monkeypatch.setattr(worktree, "_gh", _ci_gh(rollup))
    status, summary = await worktree.pr_ci_status("https://example/pr/1", cwd="/repo")
    assert status == "passing" and summary == ""


async def test_pr_ci_status_advisory_pending_does_not_hold_rollup(monkeypatch):
    """An advisory status still IN_PROGRESS can't hold the rollup pending — once every
    blocking check is green the rollup passes regardless of the advisory one."""
    rollup = (
        '[{"__typename":"CheckRun","name":"Tests","conclusion":"SUCCESS","workflowName":"CI"},'
        '{"__typename":"StatusContext","context":"coderabbit","state":"PENDING"}]'
    )
    monkeypatch.setattr(worktree, "_gh", _ci_gh(rollup))
    status, _ = await worktree.pr_ci_status("https://example/pr/1", cwd="/repo")
    assert status == "passing"


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


async def test_dispatch_coder_force_disables_managed_git(monkeypatch):
    """The board owns the git lifecycle (worktree/branch/commit/push/PR). A delegate
    configured `manage_git: true` (ADR 0076) dispatched by the loop must reach the
    adapter with it OFF — otherwise the adapter runs a second full git lifecycle on
    top of the board's and every feature yields duplicate branches/PRs."""

    @dataclasses.dataclass
    class _ManagedCoder:
        workdir: str = ""
        manage_git: bool = True

    seen = []

    class _Acp:
        async def forget_session(self, scoped):
            return True

        async def dispatch(self, scoped, prompt, *, timeout=None):
            seen.append(scoped)
            return "built it"

        async def teardown(self, scoped):
            pass

    _inject_fake_delegates(monkeypatch, _Acp())
    out = await worktree.dispatch_coder(_ManagedCoder(), "/wt", "do it", timeout=5)
    assert out == "built it"
    assert seen[0].manage_git is False  # the scoped copy, not the registry delegate
    assert seen[0].workdir == "/wt"


async def test_dispatch_coder_tolerates_delegates_without_manage_git(monkeypatch):
    """Hosts predating ADR 0076 have no `manage_git` field on Delegate — the
    force-disable must be guarded, not a blind dataclasses.replace kwarg."""

    class _Acp:
        async def forget_session(self, scoped):
            return True

        async def dispatch(self, scoped, prompt, *, timeout=None):
            return "built it"

        async def teardown(self, scoped):
            pass

    _inject_fake_delegates(monkeypatch, _Acp())
    out = await worktree.dispatch_coder(_Coder(), "/wt", "do it", timeout=5)
    assert out == "built it"


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


# ── remove_worktree / reap_feature_worktree: orphan fallback via shutil.rmtree ──────


async def test_reap_feature_worktree_rmtree_fallback_when_git_metadata_gone(monkeypatch, tmp_path):
    """When git worktree remove fails with 'is not a working tree' (git metadata is gone
    but the directory is still on disk), reap_feature_worktree must fully remove the
    directory via shutil.rmtree and return True. (r1, r4)"""
    wt_root = tmp_path / ".worktrees"
    wt_dir = wt_root / "feat-bd-147"
    wt_dir.mkdir(parents=True)
    (wt_dir / "some_code.py").write_text("# leftover")

    calls = []

    async def _fake_git(repo, *args, timeout=60):
        calls.append(args)
        if args[:2] == ("worktree", "remove"):
            return (1, "", "fatal: '/some/path' is not a working tree")
        return (0, "", "")  # prune, branch -D, etc. all succeed

    monkeypatch.setattr(worktree, "_git", _fake_git)
    result = await worktree.reap_feature_worktree(str(tmp_path), ".worktrees", "bd-147")

    assert result is True, "reap_feature_worktree must return True when directory is gone"
    assert not wt_dir.exists(), "the orphaned directory must be fully removed"
    assert any(a[:2] == ("worktree", "prune") for a in calls), "git worktree prune must be called"


async def test_remove_worktree_does_not_rmtree_on_other_git_failure(monkeypatch, tmp_path, caplog):
    """When git worktree remove fails for a reason OTHER than 'is not a working tree'
    (e.g. dirty tree, locked), the directory must NOT be touched and a WARNING must be
    logged. (r5)"""
    wt_dir = tmp_path / "feat-bd-999"
    wt_dir.mkdir()
    (wt_dir / "dirty_file.py").write_text("# uncommitted change")

    async def _fake_git(repo, *args, timeout=60):
        if args[:2] == ("worktree", "remove"):
            return (1, "", "fatal: 'path' contains modified or untracked files, use --force to delete it")
        return (0, "", "")

    monkeypatch.setattr(worktree, "_git", _fake_git)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.project_board"):
        result = await worktree.remove_worktree(str(tmp_path), str(wt_dir))

    assert result is False, "must return False when removal was not possible"
    assert wt_dir.exists(), "directory must NOT be touched when git fails for another reason"
    assert "failed" in caplog.text, "a WARNING about the failure must be logged"


# ── reap_feature_worktree: candidate sweep on cancel (#175) ─────────────────────


async def test_reap_sweeps_candidate_when_canonical_absent(monkeypatch, tmp_path):
    """Mid-first-generation cancel: only `feat-<id>.g1` exists (nothing promoted
    yet). The reap must remove the candidate worktree + its `feat/<id>.g1` branch —
    the old canonical-only reap silently no-opped here."""
    (tmp_path / ".worktrees" / "feat-bd-9.g1").mkdir(parents=True)
    calls = []

    async def _remove(repo, wt, branch=""):
        calls.append((wt, branch))
        return True

    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    await worktree.reap_feature_worktree(str(tmp_path), ".worktrees", "bd-9")
    assert calls == [
        (str(tmp_path / ".worktrees" / "feat-bd-9"), "feat/bd-9"),
        (str(tmp_path / ".worktrees" / "feat-bd-9.g1"), "feat/bd-9.g1"),
    ]


async def test_reap_sweeps_all_candidates_and_skips_other_features(monkeypatch, tmp_path):
    """Multiple candidates (`.g1`, `.c1`, `.c2`) are all cleaned in one reap call; a
    sibling feature's candidate (`feat-bd-90.g1`) and a non-candidate dot dir
    (`feat-bd-9.gx`) are left alone."""
    root = tmp_path / ".worktrees"
    for name in ("feat-bd-9.g1", "feat-bd-9.c1", "feat-bd-9.c2", "feat-bd-90.g1", "feat-bd-9.gx"):
        (root / name).mkdir(parents=True)
    calls = []

    async def _remove(repo, wt, branch=""):
        calls.append((os.path.basename(wt), branch))
        return True

    monkeypatch.setattr(worktree, "remove_worktree", _remove)
    await worktree.reap_feature_worktree(str(tmp_path), ".worktrees", "bd-9")
    assert calls[0] == ("feat-bd-9", "feat/bd-9")  # canonical attempt first, unchanged
    assert calls[1:] == [
        ("feat-bd-9.c1", "feat/bd-9.c1"),
        ("feat-bd-9.c2", "feat/bd-9.c2"),
        ("feat-bd-9.g1", "feat/bd-9.g1"),
    ]


async def test_reap_removes_candidate_dirs_from_disk_and_logs(monkeypatch, tmp_path, caplog):
    """End-to-end over the rmtree fallback: the canonical AND candidate dirs both
    vanish from disk, and the reap logs what it cleaned at INFO."""
    root = tmp_path / ".worktrees"
    canon = root / "feat-bd-9"
    cand = root / "feat-bd-9.g1"
    for d in (canon, cand):
        d.mkdir(parents=True)
        (d / "leftover.py").write_text("# stale")

    async def _fake_git(repo, *args, timeout=60):
        if args[:2] == ("worktree", "remove"):
            return (1, "", "fatal: 'x' is not a working tree")  # metadata gone → rmtree fallback
        return (0, "", "")

    monkeypatch.setattr(worktree, "_git", _fake_git)
    with caplog.at_level(logging.INFO, logger="protoagent.plugins.project_board"):
        result = await worktree.reap_feature_worktree(str(tmp_path), ".worktrees", "bd-9")

    assert result is True
    assert not canon.exists() and not cand.exists()
    assert "feat-bd-9" in caplog.text and "feat-bd-9.g1" in caplog.text


async def test_reap_is_best_effort_when_nothing_exists(monkeypatch, tmp_path):
    """No worktrees root, no candidates — the reap must not raise (same best-effort
    posture as the existing canonical no-op)."""

    async def _fake_git(repo, *args, timeout=60):
        if args[:2] == ("worktree", "remove"):
            return (1, "", "fatal: 'x' is not a working tree")
        return (0, "", "")

    monkeypatch.setattr(worktree, "_git", _fake_git)
    result = await worktree.reap_feature_worktree(str(tmp_path), ".worktrees", "bd-9")
    assert result is True  # nothing on disk → the canonical dir is (vacuously) gone


async def test_reap_tolerates_a_candidate_that_fails_to_remove(monkeypatch, tmp_path):
    """A candidate whose git removal fails for a real reason (locked/dirty) is left
    in place — never an exception into the loop."""
    cand = tmp_path / ".worktrees" / "feat-bd-9.g1"
    cand.mkdir(parents=True)

    async def _fake_git(repo, *args, timeout=60):
        if args[:2] == ("worktree", "remove"):
            return (1, "", "fatal: 'x' is locked")
        return (0, "", "")

    monkeypatch.setattr(worktree, "_git", _fake_git)
    result = await worktree.reap_feature_worktree(str(tmp_path), ".worktrees", "bd-9")
    assert result is False  # canonical never existed AND its remove reported failure
    assert cand.exists()  # the locked candidate was not force-deleted behind git's back


# ── #211: close_pr / close_pr_sync — the operator-cancel edge ───────────────────────


def _gh_for_close(state="OPEN", close=(0, "✓ Closed pull request #1", "")):
    calls = []

    async def _gh(*args, cwd, timeout=60):
        calls.append((args, cwd))
        if args[:2] == ("pr", "view"):
            return (0, state + "\n", "")
        return close

    _gh.calls = calls
    return _gh


async def test_close_pr_reads_the_state_then_runs_gh_pr_close_with_the_comment(monkeypatch):
    gh = _gh_for_close()
    monkeypatch.setattr(worktree, "_gh", gh)
    ok, detail = await worktree.close_pr("https://x/pr/1", comment="cancelled by operator — see card bd-1", cwd="/repo")
    assert (ok, detail) == (True, "")
    assert gh.calls == [
        (("pr", "view", "https://x/pr/1", "--json", "state", "--jq", ".state"), "/repo"),
        (("pr", "close", "https://x/pr/1", "--comment", "cancelled by operator — see card bd-1"), "/repo"),
    ]


@pytest.mark.parametrize("state, already", [("MERGED", "already merged"), ("CLOSED", "already closed")])
async def test_close_pr_skips_a_merged_or_closed_pr(monkeypatch, state, already):
    """A blind `gh pr close` on a merged PR fails — and "close it by hand" is the wrong
    note on merged work. Read the state first; MERGED/CLOSED is a named skip."""
    gh = _gh_for_close(state=state)
    monkeypatch.setattr(worktree, "_gh", gh)
    assert await worktree.close_pr("https://x/pr/1", comment="c") == (True, already)
    assert [c[0][:2] for c in gh.calls] == [("pr", "view")]  # never `pr close`
    assert already in (worktree.PR_ALREADY_MERGED, worktree.PR_ALREADY_CLOSED)


async def test_close_pr_never_raises(monkeypatch):
    async def _gh(*args, cwd, timeout=60):
        raise worktree.WorktreeError("gh pr close timed out after 60s")

    monkeypatch.setattr(worktree, "_gh", _gh)
    ok, detail = await worktree.close_pr("https://x/pr/1", comment="c")
    assert ok is False and "timed out" in detail

    async def _gh_fail(*args, cwd, timeout=60):
        return (1, "", "GraphQL: Pull request is already closed")

    # the state read failing (rc≠0 → "") falls through to the close, which reports its own failure
    monkeypatch.setattr(worktree, "_gh", _gh_fail)
    assert await worktree.close_pr("https://x/pr/1", comment="c") == (False, "GraphQL: Pull request is already closed")


def _run_for_close(state="OPEN", close_rc=0):
    from types import SimpleNamespace

    calls = []

    def _run(argv, **kw):
        calls.append((argv, kw.get("cwd")))
        if argv[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=state + "\n", stderr="")
        return SimpleNamespace(returncode=close_rc, stdout="ok", stderr="" if close_rc == 0 else "refused")

    _run.calls = calls
    return _run


def test_close_pr_sync_shells_gh_and_never_raises(monkeypatch):
    run = _run_for_close()
    monkeypatch.setattr(worktree.subprocess, "run", run)
    assert worktree.close_pr_sync("https://x/pr/2", comment="cancelled by operator — see card bd-2", cwd="/repo") == (
        True,
        "",
    )
    assert run.calls == [
        (["gh", "pr", "view", "https://x/pr/2", "--json", "state", "--jq", ".state"], "/repo"),
        (["gh", "pr", "close", "https://x/pr/2", "--comment", "cancelled by operator — see card bd-2"], "/repo"),
    ]
    run = _run_for_close(close_rc=1)
    monkeypatch.setattr(worktree.subprocess, "run", run)
    assert worktree.close_pr_sync("https://x/pr/2", comment="c") == (False, "refused")

    def _boom(argv, **kw):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(worktree.subprocess, "run", _boom)
    ok, detail = worktree.close_pr_sync("https://x/pr/2", comment="c")
    assert ok is False and "gh" in detail


@pytest.mark.parametrize("state, already", [("MERGED", "already merged"), ("CLOSED", "already closed")])
def test_close_pr_sync_skips_a_merged_or_closed_pr(monkeypatch, state, already):
    run = _run_for_close(state=state)
    monkeypatch.setattr(worktree.subprocess, "run", run)
    assert worktree.close_pr_sync("https://x/pr/2", comment="c") == (True, already)
    assert [c[0][:3] for c in run.calls] == [["gh", "pr", "view"]]


# ── #211: a task cancel mid-`git push` / `gh pr create` kills the child ─────────────


class _HangingProc:
    """A subprocess whose communicate() never returns on its own."""

    def __init__(self):
        self.killed = False
        self.returncode = None

    async def communicate(self):
        await asyncio.Event().wait()

    def kill(self):
        self.killed = True


@pytest.mark.parametrize("runner", ["_gh", "_git"])
async def test_task_cancel_mid_subprocess_kills_the_child_and_propagates(monkeypatch, runner):
    """`asyncio.wait_for` re-raises a CancelledError but does NOT kill the child — so a
    cancel during `git push` / `gh pr create` used to let the push/create FINISH (the
    orphan PR of #211). Kill the child, then propagate."""
    proc = _HangingProc()
    spawned = []

    async def _spawn(*argv, **kw):
        spawned.append(argv)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    coro = worktree._gh("pr", "create", cwd="/wt") if runner == "_gh" else worktree._git("/wt", "push")
    task = asyncio.create_task(coro)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert spawned and not proc.killed
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed is True


async def test_kill_on_cancel_tolerates_an_already_exited_child(monkeypatch):
    class _Gone(_HangingProc):
        def kill(self):
            raise ProcessLookupError()

    proc = _Gone()

    async def _spawn(*argv, **kw):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    task = asyncio.create_task(worktree._gh("pr", "close", cwd="/wt"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):  # the cancel still propagates; no ProcessLookupError leak
        await task


# ── base_checkout_dirt: is the main checkout a fair stand-in for base? (#255) ──────


async def test_base_checkout_dirt_is_empty_for_a_clean_checkout_on_base(monkeypatch):
    git = FakeGit({"status": (0, "", ""), "rev-parse": (0, "main\n", "")})
    monkeypatch.setattr(worktree, "_git", git)
    assert await worktree.base_checkout_dirt("/repo", "main") == ""


async def test_base_checkout_dirt_reports_uncommitted_tracked_changes(monkeypatch):
    git = FakeGit({"status": (0, " M store.py\n M loop.py\n", ""), "rev-parse": (0, "main\n", "")})
    monkeypatch.setattr(worktree, "_git", git)
    dirt = await worktree.base_checkout_dirt("/repo", "main")
    assert "store.py" in dirt and "loop.py" in dirt


async def test_base_checkout_dirt_reports_a_head_that_is_not_on_base(monkeypatch):
    git = FakeGit({"status": (0, "", ""), "rev-parse": (0, "fix/some-branch\n", "")})
    monkeypatch.setattr(worktree, "_git", git)
    assert "fix/some-branch" in await worktree.base_checkout_dirt("/repo", "main")


async def test_base_checkout_dirt_ignores_untracked_files(monkeypatch):
    """Build output and scratch dirs live in every working checkout and don't change
    what the gate compiles — treating them as dirt would make the downgrade fire
    constantly and quietly turn the preflight into a no-op."""
    git = FakeGit({"status": (0, "", ""), "rev-parse": (0, "main\n", "")})
    monkeypatch.setattr(worktree, "_git", git)
    assert await worktree.base_checkout_dirt("/repo", "main") == ""
    # the untracked-files=no flag is what buys that, so pin it
    assert any("--untracked-files=no" in a for a in git.ran("status")[0])


async def test_base_checkout_dirt_returns_empty_when_git_fails(monkeypatch):
    """Unknown is NOT dirt: this check may only ever downgrade a verdict to
    indeterminate, so a broken git must not silently disable fail-closed."""
    git = FakeGit({"status": (128, "", "not a git repository")})
    monkeypatch.setattr(worktree, "_git", git)
    assert await worktree.base_checkout_dirt("/repo", "main") == ""


async def test_base_checkout_dirt_skips_the_branch_check_without_a_base(monkeypatch):
    git = FakeGit({"status": (0, "", ""), "rev-parse": (0, "whatever\n", "")})
    monkeypatch.setattr(worktree, "_git", git)
    assert await worktree.base_checkout_dirt("/repo", "") == ""
    assert git.ran("rev-parse") == []


# ── #347: the head-pinned review-gate check run (post_review_check) ───────────────


class _CheckGh:
    """A `_gh` stub for post_review_check: records every `gh` call and answers the
    ``GET …/commits/<sha>/check-runs`` lookup from `existing` (a list of {name,id}),
    then the CREATE/UPDATE. `create_rc` drives the POST/PATCH return code."""

    def __init__(self, existing=None, create_rc=0, create_err=""):
        self.calls = []  # each call's full args tuple
        self.existing = existing or []
        self.create_rc = create_rc
        self.create_err = create_err

    async def __call__(self, *args, cwd, timeout=60):
        self.calls.append(args)
        # the idempotency lookup — GET /repos/<slug>/commits/<sha>/check-runs with
        # `--jq .check_runs`, so gh emits just the array (emulated here).
        if args and args[0] == "api" and "/check-runs" in args[1] and "/commits/" in args[1]:
            import json as _json

            return (0, _json.dumps(self.existing), "")
        # the POST/PATCH that lands the verdict
        return (self.create_rc, "", self.create_err)

    def api_writes(self):
        """The CREATE/PATCH calls (the ones that mutate a check run)."""
        return [c for c in self.calls if c and c[0] == "api" and "/commits/" not in c[1]]

    @staticmethod
    def field(call, key):
        """The `-f key=value` value from a recorded gh-api call (or None)."""
        for i, a in enumerate(call):
            if a == "-f" and i + 1 < len(call) and call[i + 1].startswith(f"{key}="):
                return call[i + 1].split("=", 1)[1]
        return None


_SLUG = "o/r"
_HEAD = "0123456789abcdef0123456789abcdef01234567"  # a full 40-char sha


async def test_post_review_check_creates_a_head_pinned_check_when_none_exists(monkeypatch):
    """r1/r2/r4: a clean verdict CREATEs a `QA panel` check keyed to the exact reviewed
    head, with a success conclusion — no existing run to update."""
    gh = _CheckGh(existing=[])
    monkeypatch.setattr(worktree, "_gh", gh)
    ok = await worktree.post_review_check(
        _SLUG, _HEAD, conclusion="success", title="Review gate: clean", summary="no blocking findings", cwd="/repo"
    )
    assert ok is True
    (write,) = gh.api_writes()
    assert "--method" in write and write[write.index("--method") + 1] == "POST"
    assert write[write.index("--method") + 2] == f"/repos/{_SLUG}/check-runs"
    assert _CheckGh.field(write, "head_sha") == _HEAD  # the FULL immutable head, not truncated
    assert _CheckGh.field(write, "name") == "QA panel"
    assert _CheckGh.field(write, "conclusion") == "success"
    assert _CheckGh.field(write, "status") == "completed"


async def test_post_review_check_updates_an_existing_run_idempotently(monkeypatch):
    """r5: a `QA panel` run ALREADY on this head is PATCHed by id — never a second POST,
    so a reconcile/retry of the same verdict leaves ONE status record, not duplicates."""
    gh = _CheckGh(existing=[{"name": "other", "id": 11}, {"name": "QA panel", "id": 42}])
    monkeypatch.setattr(worktree, "_gh", gh)
    ok = await worktree.post_review_check(
        _SLUG, _HEAD, conclusion="action_required", title="changes requested", summary="findings", cwd="/repo"
    )
    assert ok is True
    (write,) = gh.api_writes()
    assert write[write.index("--method") + 1] == "PATCH"
    assert write[write.index("--method") + 2] == f"/repos/{_SLUG}/check-runs/42"  # the existing id
    # a PATCH targets the run by id — head_sha is immutable, so it must NOT be re-sent
    assert _CheckGh.field(write, "head_sha") is None
    assert _CheckGh.field(write, "conclusion") == "action_required"


async def test_post_review_check_never_posts_without_a_head_or_slug(monkeypatch):
    """r2: an unknown reviewed head (or repo) means the gate can't prove which commit it
    reviewed — so NO check is posted (never a verdict against a guessed/moved head), and
    not even the lookup fires."""
    gh = _CheckGh()
    monkeypatch.setattr(worktree, "_gh", gh)
    assert await worktree.post_review_check(_SLUG, "", conclusion="success", title="t", summary="s") is False
    assert await worktree.post_review_check("", _HEAD, conclusion="success", title="t", summary="s") is False
    assert gh.calls == []  # short-circuited before any gh call


async def test_post_review_check_returns_false_on_a_gh_failure(monkeypatch):
    """Best-effort: a failed POST (no `checks:write`, a network blip) returns False and
    never raises into the loop — the verdict still rides the bead comment."""
    gh = _CheckGh(existing=[], create_rc=1, create_err="HTTP 403: Resource not accessible")
    monkeypatch.setattr(worktree, "_gh", gh)
    ok = await worktree.post_review_check(_SLUG, _HEAD, conclusion="failure", title="t", summary="s", cwd="/repo")
    assert ok is False


async def test_post_review_check_coerces_an_unknown_conclusion_to_neutral(monkeypatch):
    """A caller typo in the conclusion can't make gh reject the POST and drop the verdict:
    an unknown value degrades to `neutral` (a landed, if uncolored, status)."""
    gh = _CheckGh(existing=[])
    monkeypatch.setattr(worktree, "_gh", gh)
    await worktree.post_review_check(_SLUG, _HEAD, conclusion="explode", title="t", summary="s", cwd="/repo")
    (write,) = gh.api_writes()
    assert _CheckGh.field(write, "conclusion") == "neutral"


async def test_post_review_check_lookup_error_falls_back_to_create(monkeypatch):
    """A check-runs lookup that can't be read (gh non-zero / bad JSON) is treated as "no
    existing run" → CREATE, never a crash: the verdict still lands (a possible duplicate is
    the safe failure, an unposted verdict is not)."""

    async def _gh(*args, cwd, timeout=60):
        if args and args[0] == "api" and "/commits/" in args[1]:
            return (1, "boom", "not found")  # the lookup fails
        return (0, "", "")

    monkeypatch.setattr(worktree, "_gh", _gh)
    assert (
        await worktree.post_review_check(_SLUG, _HEAD, conclusion="success", title="t", summary="s", cwd="/repo")
        is True
    )
