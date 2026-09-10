"""Per-feature isolation + the scoped coder dispatch (direction D4).

The worktree is the confinement boundary (no container now — it is the *only*
sandbox). Each feature gets a disposable ``git worktree`` on a fresh branch off
``base``; the coder is dispatched with its ``workdir`` overridden to that worktree
via ``dataclasses.replace`` (the registry's static ``Delegate.workdir`` is only a
default). The coder's ACP subprocess is reaped in a ``finally`` regardless of
outcome — the #1 lifecycle rule.

``open_pr`` runs inside the worktree: commit-if-dirty → empty-diff guard
(``NoChangesError``, which the loop escalates) → push → ``gh pr create`` (reusing
an existing PR on a re-dispatch). The CI signal arrives out-of-band via the board
API (``/features/{id}/ci``); this module only builds + opens the PR.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterable

from . import config

log = logging.getLogger("protoagent.plugins.project_board")


class WorktreeError(Exception):
    """A git worktree / dispatch / PR failure. The loop turns it into Blocked."""


class NoChangesError(WorktreeError):
    """The coder produced no commits/diff vs base — a *capability* failure (the
    coder didn't deliver), which the loop escalates up the tier ladder rather than
    treating as an infra error to block on."""


class CoderTimeout(WorktreeError):
    """The coder ran past its time budget (``coder_timeout_s``) and was killed — a
    *capability* failure (didn't deliver in the budget). The loop escalates it when a
    ladder exists, else Blocks; it is NOT transient-retried (re-running the same coder
    on the same prompt would likely hang again)."""


@dataclasses.dataclass(frozen=True)
class StrandedTree:
    """One worktree holding work that exists nowhere else (#405): where it is, the branch
    it was built on, and what is in it (``unpublished_work``'s summary)."""

    path: str
    branch: str
    summary: str


@dataclasses.dataclass(frozen=True)
class PreservedTree:
    """A stranded tree whose work was saved on a branch of its own before the tree was
    removed (#405) — ``preserve_worktree``'s receipt."""

    path: str  # where the tree was
    branch: str  # the branch it was built on
    ref: str  # the preservation branch: stranded/<tree id>/<UTC stamp>
    commit: str  # its tip
    summary: str  # what the tree held (``unpublished_work``)
    diffstat: str  # `git diff --shortstat` of the uncommitted part; "" when it was only commits


class StrandedWorkError(WorktreeError):
    """A worktree the board was about to replace or reap holds work that exists nowhere
    else, and saving that work to a branch FAILED (#405) — so the tree was kept, untouched.
    Carries the ``trees`` and the ``repo`` they live in.

    The only stranded tree that stops anything: one whose work is safely on a
    ``stranded/…`` branch is removed and the build goes on. This one is not a failure of
    the build either (it never started), so nothing retries, escalates or reaps on it — the
    loop blocks the card under its own ``stranded-work`` class. The message becomes that
    block's reason, the one place the recovery is written down, so it names every path,
    what is in it, why saving failed, and both ways out."""

    def __init__(self, repo: str, trees: Iterable[StrandedTree]):
        self.repo = repo
        self.trees = list(trees)
        where = "; ".join(f"{t.path} ({t.branch}): {t.summary}" for t in self.trees)
        one = self.trees[0] if len(self.trees) == 1 else None
        path, branch = (one.path, one.branch) if one else ("<path>", "<branch>")
        super().__init__(
            f"{len(self.trees)} worktree(s) hold work that exists nowhere else and could not be saved "
            f"to a branch, so the board kept them and will not build over them — {where}. Recover it "
            f"(switch the tree to a branch of your own and commit, or open a PR from it) or discard it "
            f"(`git -C {repo} worktree remove --force {path} && git -C {repo} branch -D {branch}`"
            f"{'' if one else ' for each'}), then unblock the card."
        )


async def _git(repo: str, *args: str, timeout: float = 60, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run a git command in ``repo``; return (rc, stdout, stderr). ``env`` ADDS to the
    inherited environment — the one use is a private ``GIT_INDEX_FILE``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        repo,
        *args,
        # No stdin, own process group (#423): `git commit` runs the repo's hooks, and a
        # hook is a shell tree (husky → pnpm → lint-staged) that must neither read the
        # server's stdin nor outlive a kill.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env={**os.environ, **env} if env else None,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        kill_tree(proc)
        raise WorktreeError(f"git {' '.join(args)} timed out after {timeout}s")
    except asyncio.CancelledError:
        # #211: a task cancel (the operator's cancel verb) while the child runs —
        # wait_for re-raises it but does NOT kill the child, so a `git push` would
        # finish anyway and the branch land on the remote. Kill, then propagate.
        kill_tree(proc)
        raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


# ── repo-command children: the gate, the acceptance tests, the fixups (#423) ──────────
# A repo-defined shell command (`pnpm install && pnpm run ci`, `make check`) is a TREE,
# not a process: the shell forks the package manager, which forks the test runner. Three
# things went wrong when these were spawned as a bare `create_subprocess_shell`:
#
# * They inherited the SERVER's stdin. In the desktop app that is one never-closing pipe
#   shared by every sidecar, so anything in the tree that reads stdin blocks forever —
#   and could swallow bytes meant for the server. `stdin=DEVNULL` gives it EOF instead.
# * A timeout killed only `/bin/sh`. Its children were orphaned, kept running, and kept
#   our stdout pipe open; ~15 hung `pnpm install`s piled up across two boards.
# * On Python >= 3.11 `await proc.wait()` does not return until every pipe closes, so
#   after that shell-only kill it waited on the orphan — a drive went silent for 8h.
#
# So every such child leads its OWN session (one process group we can kill whole), and
# every timeout or cancel SIGKILLs the group and reaps it on a bound — the host's own
# contract for trees it owns (protoAgent ADR 0098, `infra.proc.group_kwargs`), and what the
# registry's gate smoke already did. The price, the same one the host pays for its shell
# tool and ACP delegates: a member stopped by a signal to its process group no longer takes
# a running gate with it — the drive's cancel path does that, if shutdown reaches it.
# POSIX only: on Windows `killpg` does not exist and this degrades to killing the shell.
_REAP_TIMEOUT_S = 10.0


async def spawn_shell(cmd: str, *, cwd: str, env: dict | None = None, stdout=None, stderr=None):
    """``create_subprocess_shell`` for a repo command: no stdin, its own process group.

    ``stdout``/``stderr`` pass straight through (``PIPE`` / ``STDOUT`` / ``DEVNULL``).
    Pair it with :func:`communicate_or_kill` (or :func:`kill_tree` + :func:`reap`) so a
    timeout or cancel takes the whole tree down, not just the shell."""
    return await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )


def kill_tree(proc) -> None:
    """SIGKILL ``proc``'s whole process group — the shell AND everything it forked.

    ``spawn_shell`` makes the shell a session leader, so its pgid is its pid. Falls back
    to killing the shell alone when the group is already gone or ``killpg`` is missing."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        return
    except (AttributeError, OSError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


async def reap(proc, *, timeout: float | None = None) -> None:
    """Wait for a killed tree to exit — on a bound (``_REAP_TIMEOUT_S`` by default). A
    descendant that left the group (daemonised itself into its own session) survives the
    group kill and can hold our pipe open; after the bound it is abandoned rather than
    allowed to hang the caller."""
    bound = _REAP_TIMEOUT_S if timeout is None else timeout
    try:
        await asyncio.wait_for(proc.wait(), timeout=bound)
    except asyncio.TimeoutError:
        pid = getattr(proc, "pid", "?")
        log.warning(
            "[project_board] killed child pid %s: its pipe is still held (a descendant escaped the "
            "process group) after %ss — abandoning it",
            pid,
            bound,
        )


async def communicate_or_kill(proc, *, timeout: float | None) -> tuple[bytes | None, bytes | None]:
    """``proc.communicate()`` on a hard ``timeout``. On timeout OR cancel, kill the whole
    tree and reap it before re-raising (``asyncio.TimeoutError`` / ``CancelledError``),
    so no caller can leave a gate running behind it. A cancel used to leave the tree
    running untouched — every drive cancel and shutdown mid-gate leaked one."""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        kill_tree(proc)
        await asyncio.shield(reap(proc))
        raise


# Paths the coder writes as its OWN session scratch — the ACP/`proto` coder's private
# state (`.proto/`: session notes + memory) and editor caches (`.cursor`) — into the
# per-feature worktree (its cwd). They must never ride into the feature PR: they make the
# reviewer-facing diff noisy and leak the agent's internal session notes into the target
# repo's history (#49). ``stage_all`` excludes them so a plain ``add -A`` skips them.
CODER_SCRATCH = (".proto", ".cursor")


async def stage_all(worktree: str, *, index_file: str = "") -> tuple[int, str, str]:
    """``git add -A`` over the worktree, MINUS what the board itself put there: the coder's
    scratch (``CODER_SCRATCH``) and the ``node_modules`` links ``create_worktree`` makes.

    The single staging seam — shared by the commit path, the verify/judge diff probes and
    stranded-work preservation — so all of them see the same intended-only file set.
    Excludes via a pathspec (``:(exclude)…``) rather than ``.git/info/exclude``, so it
    mutates nothing in the repo and depends on no target-repo ``.gitignore`` entry: the
    exclusion is scoped to this one staging call. The leading ``.`` is the positive
    pathspec the excludes subtract from.

    The links need their own lookup. A ``node_modules/`` ignore pattern — the trailing-
    slash spelling most Node repos use — matches only a real directory, so the board's
    SYMLINK is not ignored and ``add -A`` committed it into the PR (#405). Only an
    untracked, un-ignored ``node_modules`` that is a symlink is excluded; a repo that
    really tracks one is untouched.

    ``index_file`` stages into that index instead of the tree's own (``GIT_INDEX_FILE``) —
    preservation's private index, which must never touch the tree's."""
    env = {"GIT_INDEX_FILE": index_file} if index_file else None
    kw = {"env": env} if env else {}
    excludes = [f":(exclude){p}" for p in CODER_SCRATCH]
    rc, out, _err = await _git(
        worktree, "ls-files", "-z", "--others", "--exclude-standard", "--", ":(glob)**/node_modules", **kw
    )
    if rc == 0:
        excludes += [f":(exclude,literal){p}" for p in out.split("\0") if p and _is_board_link(worktree, p)]
    return await _git(worktree, "add", "-A", "--", ".", *excludes, **kw)


def _is_board_link(tree: str, entry: str) -> bool:
    """Is this untracked path (a ``status`` or ``ls-files`` entry) one of the
    ``node_modules`` links ``link_node_modules`` made? A ``node_modules/`` ignore pattern — the trailing-slash
    spelling most Node repos use — matches only a real directory, so git reports the
    board's own symlink as untracked in exactly those repos."""
    rel = entry.rstrip("/")
    return os.path.basename(rel) == "node_modules" and os.path.islink(os.path.join(tree, rel))


def _is_candidate_branch(branch: str) -> bool:
    """``feat/<id>.g<n>`` / ``.c<n>`` / ``.test`` — a throwaway candidate's branch, which
    the board never pushes, rebases or merges."""
    wt_id = branch[len("feat/") :] if branch.startswith("feat/") else ""
    return bool(wt_id) and parent_feature_id(wt_id) != wt_id


async def unpublished_work(path: str, *, branch: str = "") -> str:
    """What removing the worktree at ``path`` — and deleting ``branch`` with it — would
    destroy, as a short summary; ``""`` when nothing would be lost (#405).

    Counts every uncommitted change git can see: modified, staged, deleted and untracked
    files. Not the board's own droppings: the coder's session scratch (``CODER_SCRATCH`` —
    the exclusion ``stage_all`` applies, so "clean" here means exactly "nothing a PR would
    carry") and the ``node_modules`` links ``create_worktree`` makes.

    For a CANDIDATE branch it also counts commits no other branch, tag or remote holds.
    The brief tells a coder it is edit-only, but one with a shell can commit, and ``git
    branch -D`` drops that commit as surely as ``--force`` drops a file. Candidates only:
    the board pushes, rebases and squash-merges a CANONICAL branch, so a canonical commit
    missing from every other ref is routine — a force-pushed rebase leaves the pre-rebase
    commits exactly there — and says nothing about stranded work.

    ``""`` when ``path`` is not a worktree root (no ``.git`` in it): trees live inside the
    main checkout, so git would answer for THAT repo instead. Fails toward keeping the
    tree: on a real worktree, a git that errors or times out reports it as unreadable,
    which every caller treats as work."""
    if not os.path.exists(os.path.join(path, ".git")):
        return ""
    excludes = [f":(exclude){p}" for p in CODER_SCRATCH]
    try:
        # --no-optional-locks: a probe must never take the index lock out from under a
        # tree something may still be writing to (a hung drive's coder, bd-ezs7).
        rc, out, err = await _git(
            path, "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all", "--", ".", *excludes
        )
    except WorktreeError as exc:
        return f"unreadable ({exc})"
    if rc != 0:
        return f"unreadable (git status failed: {(err or out).strip()[:160]})"
    changes = []
    for line in out.splitlines():
        # "XY PATH" — slice the status columns off each RAW line (see base_checkout_dirt).
        entry = line[3:]
        if entry and not (line.startswith("?? ") and _is_board_link(path, entry)):
            changes.append(f"{line[:2].strip()} {entry}")
    parts = []
    if changes:
        more = f", … ({len(changes) - 5} more)" if len(changes) > 5 else ""
        parts.append(f"{len(changes)} uncommitted file(s): {', '.join(changes[:5])}{more}")
    if _is_candidate_branch(branch):
        try:
            rc, out, _err = await _git(
                path,
                "rev-list",
                "--count",
                f"refs/heads/{branch}",
                "--not",
                f"--exclude={branch}",
                "--branches",
                "--remotes",
                "--tags",
            )
        except WorktreeError as exc:
            return "; ".join([*parts, f"unreadable ({exc})"])
        n = int(out.strip()) if rc == 0 and out.strip().isdigit() else 0  # no branch → nothing to drop
        if n:
            parts.append(f"{n} commit(s) on {branch} that no other branch or remote holds")
    return "; ".join(parts)


# Where a stranded tree's work goes before the tree is removed (#405): one NEW branch per
# tree and moment, `stranded/<tree id>/<UTC stamp>`. Outside the board's `feat/` namespace,
# so no reap, rebuild or `branch -D` of the board's ever reaches it.
STRANDED_REF_PREFIX = "stranded/"


def _stamp() -> str:
    """The UTC stamp a preservation branch is named with. A seam so a test can force the
    collision ``preserve_worktree`` must refuse rather than overwrite."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


async def preserve_worktree(repo: str, path: str, branch: str, *, summary: str = "") -> PreservedTree:
    """Save everything the tree at ``path`` holds that exists nowhere else onto a NEW branch,
    ``stranded/<tree id>/<UTC stamp>``, without touching the tree, its index or its branch —
    and prove it landed (#405). Raises ``WorktreeError`` on any failure: the caller must then
    KEEP the tree, because work that could not be saved must never be destroyed.

    The commit is the tree's HEAD plus its working state, staged through ``stage_all`` into
    a PRIVATE index (``GIT_INDEX_FILE``, seeded from a copy of the tree's own for its stat
    cache). So it carries exactly what a PR would — tracked and untracked changes, no coder
    scratch, no board ``node_modules`` link — and the tree's index is never written.
    ``commit-tree`` rather than ``commit``: no hooks run (a repo's husky tree has no
    business in a salvage), ``--no-gpg-sign`` means no signing prompt can hang it, and the
    identity is pinned so a repo with none configured can still save. A tree whose only
    unique work is commits (a coder that committed on its candidate branch) gets the branch
    at its HEAD, with no new commit.

    ``update-ref`` with an empty old value creates the ref only if it does not exist yet, so
    an existing branch is never overwritten. The ref is then read back and its tree compared
    with the one just written: that proof, not an exit code, is what lets a caller remove
    the tree."""
    name = os.path.basename(os.path.normpath(path))
    ref = f"{STRANDED_REF_PREFIX}{_wt_id_from_dirname(name) if name.startswith('feat-') else name}/{_stamp()}"
    head = _checked(await _git(path, "rev-parse", "--verify", "HEAD"), path, "reading HEAD")
    scratch = tempfile.mkdtemp(prefix="pb-stranded-")
    try:
        index = os.path.join(scratch, "index")
        env = {"GIT_INDEX_FILE": index}
        own = _checked(await _git(path, "rev-parse", "--git-path", "index"), path, "locating the index")
        own = own if os.path.isabs(own) else os.path.join(path, own)
        if os.path.exists(own):
            shutil.copyfile(own, index)
        else:
            _checked(await _git(path, "read-tree", "HEAD", env=env), path, "seeding a private index")
        _checked(await stage_all(path, index_file=index), path, "staging")
        tree = _checked(await _git(path, "write-tree", env=env), path, "writing the tree")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    commit, diffstat = head, ""
    if tree != _checked(await _git(path, "rev-parse", "--verify", f"{head}^{{tree}}"), path, "reading HEAD's tree"):
        message = (
            f"stranded work from {path} ({branch or 'no branch'})\n\n"
            f"Saved by project-board before the tree was removed (#405). It held: {summary or 'unknown'}"
        )
        commit = _checked(
            await _git(
                path,
                "-c",
                "user.name=project-board",
                "-c",
                "user.email=project-board@localhost",
                "commit-tree",
                "--no-gpg-sign",
                "-p",
                head,
                "-m",
                message,
                tree,
            ),
            path,
            "committing",
        )
        diffstat = _checked(await _git(path, "diff", "--shortstat", head, commit), path, "measuring the diff")
    _checked(await _git(path, "update-ref", f"refs/heads/{ref}", commit, ""), path, f"creating {ref}")
    if _checked(await _git(path, "rev-parse", "--verify", f"refs/heads/{ref}^{{tree}}"), path, "reading back") != tree:
        raise WorktreeError(f"preserving {path}: {ref} does not hold the tree's state")
    return PreservedTree(os.path.abspath(path), branch, ref, commit, summary, diffstat)


def _checked(result: tuple[int, str, str], path: str, step: str) -> str:
    """A ``_git`` result's stripped stdout — or, on a non-zero exit, the ``WorktreeError``
    that makes ``preserve_worktree``'s caller keep the tree."""
    rc, out, err = result
    if rc != 0:
        raise WorktreeError(f"preserving {path}: {step} failed: {(err or out).strip()[:200]}")
    return out.strip()


def preserved_note(repo: str, preserved: Iterable[PreservedTree], *, base: str = "") -> str:
    """The card comment for trees whose work was saved and then removed (#405): each
    branch, what it holds, and how to look at it or take it back. ``base`` (the card's base
    branch) makes the inspect/salvage commands exact; without it they fall back to the
    preserved commit alone."""
    trees = list(preserved)
    lines = [
        f"stranded work preserved: {len(trees)} worktree(s) held changes that existed nowhere else. "
        f"Each is saved on a branch of its own, and the tree was removed so the card can build again."
    ]
    for t in trees:
        held = t.diffstat or "commits only"
        lines.append(f"- {t.ref} — from {t.path} ({t.branch}): {held}; it held {t.summary}")
    ref = trees[0].ref if len(trees) == 1 else "<branch>"
    if base:
        lines.append(f"Inspect: git -C {repo} diff origin/{base}...{ref}")
        lines.append(f"Salvage: git -C {repo} cherry-pick origin/{base}..{ref} — or push {ref} and open a PR from it.")
    else:
        lines.append(f"Inspect: git -C {repo} show --stat {ref}")
        lines.append(f"Salvage: cherry-pick its commits onto your branch, or push {ref} and open a PR from it.")
    lines.append(f"Delete it once it is no longer needed: git -C {repo} branch -D {ref}")
    return "\n".join(lines)


def slugify(title: str, max_len: int = 40) -> str:
    """A filesystem/branch-safe slug of a feature title (#227): lowercased, every run of
    non-alphanumerics collapsed to a single hyphen, leading/trailing hyphens stripped,
    truncated to ``max_len`` chars (then a hyphen the cut left dangling is stripped).
    Returns ``""`` for a title that is empty or all-punctuation — the branch/dir helpers
    fall back to the bare ``feat-<fid>`` shape in that case."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s


def branch_name(fid: str, title: str = "") -> str:
    """The git branch for a feature build: ``feat/<fid>-<slug>`` (#227), or the bare
    ``feat/<fid>`` when the title slugs to nothing. The ``<fid>`` is the machine key at
    the front — every recovery/parsing path keys off it; the ``-<slug>`` is human sugar
    for reviewers reading a branch list."""
    slug = slugify(title)
    return f"feat/{fid}-{slug}" if slug else f"feat/{fid}"


def worktree_dir(fid: str, title: str = "") -> str:
    """The per-feature worktree directory basename — ``feat-<fid>-<slug>`` (#227), or the
    bare ``feat-<fid>`` when the title slugs to nothing. Mirrors ``branch_name`` (``/`` →
    ``-``) so a worktree dir and its branch share the same ``<fid>-<slug>`` tail."""
    slug = slugify(title)
    return f"feat-{fid}-{slug}" if slug else f"feat-{fid}"


async def base_checkout_dirt(repo: str, base: str = "") -> str:
    """Why ``repo``'s MAIN checkout is not a faithful stand-in for the base branch — ''
    when it is one.

    The gate preflight smoke-runs a project's gate with ``cwd=<repo>`` on the premise
    that coders only ever touch worktrees, so the main checkout still sits at base. That
    premise is about the CODERS; it says nothing about the operator, who edits that same
    checkout by hand. When it doesn't hold, the preflight's verdict is about the
    operator's uncommitted work rather than about the base every worktree branches from
    — which can silently freeze a whole project (a local edit that reddens the gate) or
    silently clear a genuinely broken one.

    Reports two kinds of dirt, cheaply (two plumbing calls, no fetch, no network):
    uncommitted tracked changes, and a HEAD that isn't on ``base``. Untracked files are
    NOT dirt — build output and scratch dirs live in every working checkout and don't
    change what the gate compiles. A git failure returns '' (unknown → not dirt): this
    check may only ever downgrade a verdict to indeterminate, never invent one."""
    try:
        rc, out, _err = await _git(repo, "status", "--porcelain", "--untracked-files=no")
        if rc != 0:
            return ""
        reasons = []
        if out.strip():
            # porcelain v1 is "XY PATH" — slice the 2 status columns off each line as it
            # comes. NOT off a pre-stripped block: that eats the first line's leading
            # space and takes a character of the filename with it (" M store.py" then
            # reads as "tore.py").
            files = [ln[2:].strip() for ln in out.splitlines() if ln[2:].strip()][:5]
            reasons.append(f"uncommitted changes to {', '.join(files)}")
        if base:
            rc_b, head, _e = await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            if rc_b == 0 and head.strip() and head.strip() != base:
                reasons.append(f"HEAD is on {head.strip()!r}, not the base branch {base!r}")
        return "; ".join(reasons)
    except Exception:  # noqa: BLE001 — an unavailable git must not manufacture dirt
        return ""


async def prune_stale_worktrees(repo: str) -> str:
    """``git worktree prune -v`` in ``repo`` — drop stale ``.git/worktrees/*`` admin
    entries whose working tree is gone (a branch merged + its tree deleted, or a tree
    created outside the loop and later removed by hand). Verbose so the pruned entries
    are surfaced; when it cleans anything, it is logged at WARNING (#225): a corrupt
    worktree admin dir is exactly what makes git refuse a fresh ``worktree add`` with
    'fatal: not a git repository'. Best-effort — a non-zero prune is ignored (the
    subsequent ``worktree add`` reports the real error). Returns the stripped output."""
    _rc, out, _err = await _git(repo, "worktree", "prune", "-v")
    out = out.strip()
    if out:
        log.warning("[project_board] pruned stale worktree(s) in %s:\n%s", repo, out)
    return out


async def create_worktree(
    repo: str, base: str, fid: str, root: str = ".worktrees", title: str = "", *, resume: bool = False
) -> tuple[str, str]:
    """``git worktree add <root>/feat-<id>[-<slug>] -b feat/<id>[-<slug>] <base>``.

    Returns (absolute worktree path, branch). The branch is fresh off ``base`` so
    the blast radius is one throwaway tree. Cleans a stale worktree/branch of the
    same name first (idempotent re-run after a crashed feature) — after saving any work
    that tree holds that exists nowhere else to a ``stranded/…`` branch
    (``preserve_worktree``), or raising ``StrandedWorkError`` and leaving it untouched when
    saving fails (#405).

    ``resume`` (a FIX ROUND on a card that already has an open PR) starts from
    ``origin/<branch>`` instead — the PR head — when that ref resolves. Without it the
    coder is handed a clean tree off base while the prompt tells it to "fix every finding
    in the existing branch": the prompt and the filesystem disagree, and the coder must
    re-implement the whole change before it can address a one-line finding. On a large
    card that does not fit in the dispatch timeout at all (three 30-minute rounds, zero
    commits, observed live). A missing remote branch falls back to ``base`` — a card
    whose branch was deleted still builds rather than failing the dispatch.

    Preventively runs ``git worktree prune`` before the add (#225): stale
    ``.git/worktrees/*`` entries left behind outside the loop — branches merged and
    their trees deleted by hand — corrupt the worktree state enough that git refuses a
    fresh ``worktree add`` with 'fatal: not a git repository' (observed for the
    release-tools checkout). If the add still fails with a git error, prune again and
    retry it ONCE before blocking.

    ``title`` (#227) is slugged onto the canonical branch/dir tail for readability;
    throwaway candidate worktrees (``.g<n>``/``.c<k>``) pass none, keeping the bare
    ``feat-<cid>`` shape the candidate-suffix stripping relies on."""
    branch = branch_name(fid, title)
    rel = os.path.join(root, worktree_dir(fid, title))
    path = os.path.join(repo, rel)
    # Preventive: drop stale worktree admin entries before touching anything (#225).
    await prune_stale_worktrees(repo)
    # The cleanup below is `remove --force` + `branch -D`. On a tree whose coder died
    # before promotion, that is the only copy of the work — bd-ezs7's finished 170 lines
    # survived only because a hung drive kept the card from re-dispatching (#405). So a
    # tree holding work is saved to a `stranded/…` branch first, or refused if that fails.
    # (The drive sets such trees aside itself, with a card comment, before it asks for a
    # fresh tree; this is the backstop for every other caller.)
    await _preserve_or_refuse(repo, path, branch, "replacing")
    # Best-effort cleanup of a prior run's leftovers.
    await _git(repo, "worktree", "remove", "--force", rel)
    await _git(repo, "branch", "-D", branch)
    # Branch off the LATEST remote base. Two-branch repos put features on `dev`,
    # which the local clone may not even have as a branch; and even when it does, a
    # stale local ref would build off old code. Fetch best-effort, then start from
    # origin/<base> if it resolves, else the local <base> (the no-remote case). The
    # PR base stays the plain `<base>` in open_pr — worktree-base and PR-base are decoupled.
    await _git(repo, "fetch", "origin", base)
    start = f"origin/{base}"
    if resume:
        # Best-effort: fetch the feature branch and start from it when it exists, so the
        # coder opens its own prior work instead of a clean base.
        await _git(repo, "fetch", "origin", branch)
        rc, _out, _err = await _git(repo, "rev-parse", "--verify", "--quiet", f"origin/{branch}")
        if rc == 0:
            start = f"origin/{branch}"
            log.info("[project_board] %s resuming existing branch %s for a fix round", fid, branch)
        else:
            log.info(
                "[project_board] %s asked to resume %s but no remote branch exists — building off %s",
                fid,
                branch,
                base,
            )
    rc_chk, _o, _e = await _git(repo, "rev-parse", "--verify", "--quiet", start)
    if rc_chk != 0:
        start = base
    rc, _out, err = await _git(repo, "worktree", "add", rel, "-b", branch, start)
    if rc != 0:
        # A git error here (classically 'fatal: not a git repository' out of a corrupt
        # worktree admin dir, #225) — prune the stale references and retry the add ONCE.
        # A single retry, not a loop: if the tree still won't create, the loop blocks.
        log.warning(
            "[project_board] worktree add failed for %s (%s) — pruning stale worktrees and retrying once",
            rel,
            err.strip()[:200],
        )
        await prune_stale_worktrees(repo)
        rc, _out, err = await _git(repo, "worktree", "add", rel, "-b", branch, start)
        if rc != 0:
            raise WorktreeError(f"worktree add failed: {err.strip()[:300]}")
    abspath = os.path.abspath(path)
    # A fresh worktree is a bare checkout with NO node_modules, so an npm/pnpm pre-PR gate
    # (or the coder running the build) can't resolve deps. Symlink the main repo's
    # node_modules in (best-effort, no-op for non-node repos) rather than a slow/offline
    # per-worktree install.
    await asyncio.to_thread(link_node_modules, repo, abspath)
    return abspath, branch


def link_node_modules(repo: str, worktree: str) -> int:
    """Symlink every ``node_modules`` dir in the main repo into the worktree at the same
    relative path (handles monorepos — root + each workspace package). The worktree shares
    the repo's installed deps, so npm/pnpm gates + builds resolve without a per-worktree
    install. Best-effort: a non-node repo (no node_modules) is a no-op; symlink failures are
    skipped. Build output (dist/, etc.) still lands in the worktree — only the deps are
    shared. Returns the number linked."""
    linked = 0
    try:
        for root, dirs, _files in os.walk(repo):
            if "node_modules" in dirs:
                rel = os.path.relpath(os.path.join(root, "node_modules"), repo)
                src = os.path.join(repo, rel)
                dst = os.path.join(worktree, rel)
                try:
                    if not os.path.lexists(dst):
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        os.symlink(src, dst)
                        linked += 1
                except OSError:
                    pass
            # Don't descend into node_modules / git internals / sibling worktrees.
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", ".worktrees")]
    except OSError:
        pass
    return linked


async def remove_worktree(repo: str, worktree: str, branch: str = "") -> bool:
    """Tear down the worktree (and its branch, once merged the branch is junk).

    Returns True if the worktree directory is gone after the call, False otherwise.
    Best-effort — teardown must not raise into the loop's success path.

    When ``git worktree remove`` fails because the git metadata is already gone
    (stderr contains "is not a working tree"), the directory may still be on disk.
    In that case: prune stale admin entries, then remove the directory via
    ``shutil.rmtree``, and return True only if the directory is now absent.
    Any other failure reason (dirty tree, locked, permissions) returns False
    without touching the directory."""
    rc, _out, err = await _git(repo, "worktree", "remove", "--force", worktree)
    removed = rc == 0
    if not removed:
        if "is not a working tree" in err:
            await _git(repo, "worktree", "prune")
            try:
                shutil.rmtree(worktree)
            except OSError:
                pass
            removed = not os.path.exists(worktree)
        else:
            log.warning("[project_board] worktree remove %s failed: %s", worktree, err.strip()[:200])
    if branch:
        await _git(repo, "branch", "-D", branch)
    return removed


def _feature_tree_names(base: str, fid: str) -> tuple[list[str], list[str]]:
    """The on-disk worktree dirs under ``base`` that feature ``fid`` owns BY SCAN, as
    ``(slugged canonical names, candidate names)``, sorted. The bare ``feat-<fid>`` is
    not scanned for — its name is computable, and ``reap_feature_worktree`` attempts it
    whether or not it exists.

    Slugged canonical: ``feat-<fid>-<slug>`` (#227), a hyphen tail. Candidate:
    ``feat-<fid>.<suffix>`` whose suffixes strip back to ``fid`` (the stacked
    ``.test.g2`` shape included; ``feat-<fid>x`` prefix-collisions and non-candidate dots
    like ``.gx`` excluded). A candidate uses a ``.`` separator, so neither scan can
    mistake one kind for the other. Branches mirror the names (``feat-`` → ``feat/``)."""
    try:
        names = sorted(n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n)))
    except OSError:
        return [], []
    slugged = [n for n in names if n.startswith(f"feat-{fid}-")]
    candidates = [n for n in names if n.startswith(f"feat-{fid}.") and parent_feature_id(n[len("feat-") :]) == fid]
    return slugged, candidates


async def _save(repo: str, path: str, branch: str, summary: str, why: str) -> PreservedTree | StrandedTree:
    """``preserve_worktree``, logged: the receipt on success, or — when saving failed — the
    ``StrandedTree`` (reason included) that the caller must keep rather than destroy."""
    try:
        saved = await preserve_worktree(repo, path, branch, summary=summary)
    except WorktreeError as exc:
        return StrandedTree(os.path.abspath(path), branch, f"{summary} — saving it to a branch failed: {exc}")
    log.warning(
        "[project_board] %s held work that existed nowhere else (%s) — saved it to %s before %s it (#405)",
        path,
        summary,
        saved.ref,
        why,
    )
    return saved


async def _preserve_or_refuse(repo: str, path: str, branch: str, why: str) -> PreservedTree | None:
    """Before ``create_worktree`` / ``promote_worktree`` force-clear the tree at ``path``:
    save whatever it holds that exists nowhere else to a ``stranded/…`` branch (#405).
    ``None`` when it held nothing; raises ``StrandedWorkError`` — leaving the tree exactly as
    it is — when there was work and saving it failed."""
    stranded = await unpublished_work(path, branch=branch)
    if not stranded:
        return None
    saved = await _save(repo, path, branch, stranded, why)
    if isinstance(saved, StrandedTree):
        raise StrandedWorkError(repo, [saved])
    return saved


async def _reap_saving(repo: str, path: str, branch: str, kept: list[StrandedTree]) -> bool:
    """``remove_worktree``, after first saving any work the tree holds that exists nowhere
    else to a ``stranded/…`` branch (#405). If saving fails, the tree is left exactly as it
    is, recorded in ``kept``, and reported not-removed."""
    stranded = await unpublished_work(path, branch=branch)
    if stranded:
        saved = await _save(repo, path, branch, stranded, "reaping")
        if isinstance(saved, StrandedTree):
            kept.append(saved)
            return False
    return await remove_worktree(repo, path, branch)


# (path, summary) pairs already reported by ``_warn_kept`` in this process. The health
# sweep re-offers a kept tree every pass; the operator needs to hear about it once, and
# again only if what is in it changes.
_KEPT_WARNED: set[tuple[str, str]] = set()


def _warn_kept(fid: str, kept: list[StrandedTree]) -> None:
    for t in kept:
        if (t.path, t.summary) in _KEPT_WARNED:
            log.debug("[project_board] %s: still keeping %s — its work could not be saved", fid, t.path)
            continue
        _KEPT_WARNED.add((t.path, t.summary))
        log.warning(
            "[project_board] %s: KEPT %s (%s) — it holds work that exists nowhere else and could not be "
            "saved to a branch: %s. The board will not reap it; recover or discard it by hand (#405)",
            fid,
            t.path,
            t.branch,
            t.summary,
        )


async def reap_feature_worktree(repo: str, worktrees_root: str, fid: str) -> bool:
    """Remove the worktree(s) + branch(es) a feature owns, by its id — the one place
    that knows the ``feat-<id>`` / ``feat/<id>`` naming. Shared by the merge webhook,
    the merge poll (both reap once a feature reaches ``done``), the cancel path, and the
    health sweep.

    Reaps the canonical ``feat-<id>`` tree first, then sweeps any leftover CANDIDATE
    trees (``feat-<id>.g<n>`` / ``.c<n>`` / ``.test``…, the ``_CANDIDATE_SUFFIX_RE``
    shapes): a feature cancelled mid-first-generation has no canonical worktree yet —
    only candidates — and a canonical-only reap would silently no-op, stranding the
    tree + branch on disk (#175). Each candidate's branch follows the same
    ``feat/<id>.<suffix>`` naming it was created with. Best-effort throughout (an
    already-gone candidate is simply skipped). Returns True if the canonical
    directory is gone after the call.

    The canonical tree may carry a human ``-<slug>`` tail (#227) — ``feat-<id>-<slug>``,
    whose slug isn't recomputable from ``fid`` alone here. The bare ``feat-<id>`` name is
    always ATTEMPTED (an idempotent no-op when only candidates or a slugged tree exist),
    then any on-disk ``feat-<id>-*`` slugged variant is discovered by scan and removed
    with its matching ``feat/<id>-<slug>`` branch.

    Every caller is cleaning up after a run that is already over, and a coder that died
    before promotion leaves its only copy of the work exactly here — so a tree still holding
    work that exists nowhere else (``unpublished_work``) is saved to a ``stranded/…`` branch
    before it is reaped, and logged with that branch (#405). If saving fails the tree is
    KEPT, logged by path once, never destroyed. A drive discarding the trees it just built
    itself — rejected candidates — removes them by path with ``remove_worktree``, never
    through here."""
    base = os.path.join(repo, worktrees_root)
    canonical = os.path.join(base, f"feat-{fid}")
    kept: list[StrandedTree] = []
    had_canonical = os.path.isdir(canonical)
    removed = await _reap_saving(repo, canonical, f"feat/{fid}", kept)
    cleaned: list[str] = [f"feat-{fid}"] if (removed and had_canonical) else []
    slugged, candidates = _feature_tree_names(base, fid)
    for name in slugged:
        if await _reap_saving(repo, os.path.join(base, name), "feat/" + name[len("feat-") :], kept):
            cleaned.append(name)
        else:
            removed = False
    reaped: list[str] = []
    for name in candidates:
        if await _reap_saving(repo, os.path.join(base, name), "feat/" + name[len("feat-") :], kept):
            reaped.append(name)
    if cleaned or reaped:
        log.info("[project_board] reaped worktrees for %s: %s", fid, ", ".join(cleaned + reaped))
    if kept:
        _warn_kept(fid, kept)
    return removed


async def stranded_worktrees(repo: str, worktrees_root: str, fid: str) -> list[StrandedTree]:
    """Every tree feature ``fid`` has on disk that holds work existing nowhere else (#405)
    — its canonical ``feat-<id>[-<slug>]`` and every candidate ``feat-<id>.<suffix>``, by
    the naming ``reap_feature_worktree`` sweeps. Empty when removing them loses nothing."""
    base = os.path.join(repo, worktrees_root)
    slugged, candidates = _feature_tree_names(base, fid)
    trees: list[StrandedTree] = []
    for name in [f"feat-{fid}", *slugged, *candidates]:
        path = os.path.join(base, name)
        stranded = await unpublished_work(path, branch="feat/" + name[len("feat-") :])
        if stranded:
            trees.append(StrandedTree(os.path.abspath(path), "feat/" + name[len("feat-") :], stranded))
    return trees


async def set_aside_stranded_worktrees(
    repo: str, worktrees_root: str, fid: str
) -> tuple[list[PreservedTree], list[StrandedTree]]:
    """Clear the ground for a fresh build of ``fid`` without losing a line (#405): save
    every stranded tree the card owns (``stranded_worktrees``) to a ``stranded/…`` branch,
    then remove it. Returns ``(saved, unsaved)`` — ``unsaved`` are the trees whose saving
    failed; they are left exactly as they are.

    Every tree, not just the one this build will reuse: a stale ``.g2`` would otherwise
    surface mid-ladder, after the greedy rung was already spent. Removing each saved tree
    here, rather than leaving it to the next ``create_worktree``, keeps a tree this build
    never recreates from being saved a second time by a later reap."""
    saved: list[PreservedTree] = []
    unsaved: list[StrandedTree] = []
    for tree in await stranded_worktrees(repo, worktrees_root, fid):
        result = await _save(repo, tree.path, tree.branch, tree.summary, "rebuilding over")
        if isinstance(result, StrandedTree):
            unsaved.append(result)
            continue
        await remove_worktree(repo, tree.path, tree.branch)
        saved.append(result)
    return saved, unsaved


async def promote_worktree(
    repo: str, src_wt: str, src_branch: str, fid: str, root: str = ".worktrees", title: str = ""
) -> tuple[str, str]:
    """Promote a Max-Mode candidate worktree to the canonical ``feat-<id>[-<slug>]`` /
    ``feat/<id>[-<slug>]`` name (#21, #227). The N candidates build in throwaway
    ``feat-<id>.c<k>`` worktrees; the winner has to take over the canonical name so the
    rest of the lifecycle — the CI-fail bounce, crash recovery
    (``pr_url_for_branch(branch_name(<id>, title))``), and reaping
    (``reap_feature_worktree(<id>)``) — all of which key off the canonical names — works
    unchanged. ``title`` (#227) picks the same ``-<slug>`` tail ``create_worktree`` /
    ``branch_name`` would, so the promoted canonical matches what the loop recomputes.

    Moves the worktree dir and renames its branch IN PLACE, so the coder's still-
    uncommitted changes ride along (verified: ``git worktree move`` + ``branch -m``
    preserve the dirty tree). Idempotently clears a stale canonical worktree/branch
    first so ``move`` has a free destination — after saving any work that canonical tree
    holds to a ``stranded/…`` branch, or raising ``StrandedWorkError`` before anything
    moves if that fails (#405). A winner already at the canonical path is a no-op.
    Returns (canonical_path, canonical_branch)."""
    canon_branch = branch_name(fid, title)
    canon_rel = os.path.join(root, worktree_dir(fid, title))
    canon_path = os.path.join(repo, canon_rel)
    if os.path.abspath(src_wt) == os.path.abspath(canon_path):
        return os.path.abspath(canon_path), canon_branch
    # A canonical tree an earlier drive left mid-build is the same stranded work a dead
    # candidate is (#405): save it before the forced clear — and before the winner moves.
    await _preserve_or_refuse(repo, canon_path, canon_branch, "replacing")
    # Free the destination: drop any stale canonical worktree/branch leftover.
    await _git(repo, "worktree", "remove", "--force", canon_rel)
    await _git(repo, "branch", "-D", canon_branch)
    rc, _o, err = await _git(repo, "worktree", "move", os.path.abspath(src_wt), os.path.abspath(canon_path))
    if rc != 0:
        raise WorktreeError(f"worktree move failed: {err.strip()[:200]}")
    rc, _o, err = await _git(canon_path, "branch", "-m", src_branch, canon_branch)
    if rc != 0:
        raise WorktreeError(f"branch rename failed: {err.strip()[:200]}")
    return os.path.abspath(canon_path), canon_branch


# Candidate-worktree id suffixes: `.g<n>` (coder.solve candidates), `.c<n>` (Max-Mode
# candidates), `.test` (the operator-only test-rung diagnostic — whose own candidates
# stack as `.test.g<n>`). A real feature id never contains a dot, so stripping these is
# unambiguous.
_CANDIDATE_SUFFIX_RE = re.compile(r"\.(?:g\d+|c\d+|test)$")


def parent_feature_id(wt_id: str) -> str:
    """The feature id that OWNS a `feat-<wt_id>` worktree — `wt_id` itself for a
    canonical worktree, the `.gN`/`.cN`/`.test` suffixes stripped (repeatedly, for the
    stacked `bd-1.test.g2` shape) for a candidate one. The health sweep resolves board
    state through this so a leftover candidate worktree is reaped by its PARENT
    feature's state instead of warning every sweep on a non-feature id (#91)."""
    out = wt_id
    while True:
        stripped = _CANDIDATE_SUFFIX_RE.sub("", out)
        if stripped == out:
            return out
        out = stripped


# The feature id at the FRONT of a `feat-<id>[-<slug>]` worktree dir (#227): a `bd-…`
# bead id plus any `.<sub>`/`.g<n>`/`.c<n>`/`.test` dot-segments (sub-feature + candidate
# suffixes), stopping at the human slug's leading `-`. The fid body never contains a bare
# `-` (only the `bd-` prefix does) and the slug never contains a `.`, so the boundary is
# unambiguous. `parent_feature_id` then strips the candidate suffixes off what this keeps.
_FID_PREFIX_RE = re.compile(r"^(bd-[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)")


def _wt_id_from_dirname(name: str) -> str:
    """The slug-free worktree id (``<fid>`` or ``<fid>.<candidate-suffix>``) from a
    ``feat-<id>[-<slug>]`` dir name (#227) — the fid is the machine key at the front, the
    ``-<slug>`` a human suffix that recovery/parsing must ignore. Falls back to the whole
    post-``feat-`` remainder for a non-``bd`` id, preserving the pre-slug behavior."""
    tail = name[len("feat-") :]
    m = _FID_PREFIX_RE.match(tail)
    return m.group(1) if m else tail


def list_feature_worktrees(repo: str, worktrees_root: str) -> list[str]:
    """The feature ids that currently have a ``feat-<id>[-<slug>]`` worktree dir under
    ``<repo>/<worktrees_root>`` — for the health sweep's orphan check. The human slug tail
    (#227) is stripped back to the machine ``<id>`` so the sweep resolves board state by
    fid, not by the slugged dir name. Sync (a quick dir listing); returns ``[]`` if the
    dir is absent."""
    base = os.path.join(repo, worktrees_root)
    try:
        names = os.listdir(base)
    except OSError:
        return []
    return [_wt_id_from_dirname(n) for n in names if n.startswith("feat-") and os.path.isdir(os.path.join(base, n))]


async def dispatch_coder(
    coder, worktree: str, prompt: str, *, timeout: float | None = None, env_passthrough: Iterable[str] = ()
) -> str:
    """Dispatch the coder (an ``acp`` Delegate) scoped to ``worktree``.

    Builds a per-feature copy with the worktree as workdir (registry untouched),
    dispatches via the adapter, and ALWAYS tears the ACP subprocess down — the
    cache keys on workdir, so each feature owns a distinct client that must be
    reaped here, not left to pile up.

    Fresh-both: every attempt gets a freshly recreated worktree (``create_worktree``
    wipes + rebuilds it off the base), so the coder must also start a FRESH ACP
    session. Otherwise a re-dispatch (CI-fail bounce, tier escalation, crash
    recovery) would ``session/load``-resume a thread whose memory references a diff
    the wiped tree no longer has — the coder thinks it's already done (→ no diff) or
    edits against stale assumptions. Forgetting the session first keeps its memory in
    step with the empty tree. (A first attempt has no session to forget → no-op.)

    The BOARD owns the git lifecycle for scoped dispatches — worktree, branch,
    commit, push, PR (this module). A delegate configured with ``manage_git: true``
    (ADR 0076's harness-owned lifecycle for direct ``delegate_to`` dispatches) must
    NOT keep it here: the adapter would run a second branch/commit/push/PR on top of
    the board's, yielding duplicate PRs. Force-disable it on the scoped copy
    (guarded, so hosts predating the field still work)."""
    from plugins.delegates.adapters import ADAPTERS, DelegateError

    adapter = ADAPTERS["acp"]
    overrides: dict = {"workdir": worktree}
    if any(f.name == "manage_git" for f in dataclasses.fields(coder)):
        overrides["manage_git"] = False
    if any(f.name == "env" for f in dataclasses.fields(coder)):
        overrides["env"] = config.sanitized_env(env_passthrough)
    scoped = dataclasses.replace(coder, **overrides)
    try:
        await adapter.forget_session(scoped)
    except Exception:  # noqa: BLE001 — best-effort; a stale session must not block the build
        log.warning("[project_board] forget_session failed for %s", worktree, exc_info=True)
    try:
        # Hard-bound the dispatch so a hung coder can't hold a worktree/slot forever.
        # On timeout asyncio.wait_for cancels the dispatch — the finally below reaps
        # the subprocess — and we raise CoderTimeout (capability, not transient).
        coro = adapter.dispatch(scoped, prompt, timeout=timeout)
        return await (asyncio.wait_for(coro, timeout) if timeout else coro)
    except asyncio.TimeoutError:
        raise CoderTimeout(f"coder timed out after {timeout}s")
    except DelegateError as exc:
        raise WorktreeError(f"coder dispatch failed: {exc}")
    finally:
        # #1 lifecycle rule: pop AND close the worktree-scoped subprocess.
        try:
            await adapter.teardown(scoped)
        except Exception:  # noqa: BLE001 — never let teardown mask the result/error
            log.warning("[project_board] coder teardown failed for %s", worktree, exc_info=True)


async def _gh(*args: str, cwd: str, timeout: float = 60) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,  # #423: never the server's stdin
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        kill_tree(proc)
        raise WorktreeError(f"gh {' '.join(args)} timed out after {timeout}s")
    except asyncio.CancelledError:
        # #211: same as _git — a cancel mid-`gh pr create` must not let the child
        # finish and open a PR nobody owns. (If it already did, the drive's cancel
        # path finds it by branch — pr_url_for_branch — and closes it.)
        kill_tree(proc)
        raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def commit_worktree(worktree: str, message: str) -> None:
    """Commit whatever the coder left uncommitted in the worktree. No-op if the
    tree is clean (the coder may have committed its own work)."""
    _rc, out, _err = await _git(worktree, "status", "--porcelain")
    if not out.strip():
        return
    await stage_all(worktree)
    rc, o, e = await _git(worktree, "commit", "-m", message)
    if rc != 0 and "nothing to commit" not in (o + e).lower():
        raise WorktreeError(f"commit failed: {(e or o).strip()[:200]}")


async def open_pr(
    worktree: str, branch: str, *, base: str = "main", title: str, body: str = "", promote_draft: bool = True
) -> str:
    """Commit + push the worktree's branch and open (or reuse) a PR; return its URL.

    Operates **inside the worktree** (the confinement boundary). Raises
    ``NoChangesError`` if the coder produced nothing (no commits vs ``base``) — the
    loop escalates that, vs a push/`gh` failure which it treats as infra → Blocked.
    Idempotent: if a PR already exists for the branch (a re-dispatch after CI fail),
    it pushes the new commits and returns the existing PR url instead of erroring.

    ``promote_draft`` (#207): when the existing PR is a DRAFT, mark it ready — meant
    for the FIRST adoption only (the card has no ``pr_url`` yet, so the draft is the
    coder's, not the operator's). The loop passes ``False`` on a re-dispatch of a card
    that already owns a PR: an operator who converted the loop's own PR to draft as a
    hold must not have it silently un-drafted by the next CI-fail bounce."""
    # 1. Commit anything left uncommitted, then guard against an empty result.
    await commit_worktree(worktree, title)
    _rc, out, _err = await _git(worktree, "rev-list", "--count", f"{base}..HEAD")
    n = int(out.strip()) if out.strip().isdigit() else 0
    if n == 0:
        raise NoChangesError("coder produced no commits vs base — nothing to PR")

    # 2. Push the branch from the worktree. `--force-with-lease`: a re-dispatch
    #    (CI-fail bounce) builds a FRESH worktree off origin/<base>, so its history
    #    diverges from the remote `feat/<id>` branch the first attempt pushed — a
    #    plain push would be rejected (non-fast-forward) and the re-dispatch would
    #    never land. The branch is the loop's own throwaway; lease-guarded force is
    #    safe (and a no-op on the first push when the branch is new).
    rc, _o, err = await _git(worktree, "push", "-u", "--force-with-lease", "origin", branch, timeout=180)
    if rc != 0:
        raise WorktreeError(f"git push failed: {err.strip()[:300]}")

    # 3. Open the PR — or recover the existing one (re-dispatch case).
    rc, out, err = await _gh(
        "pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body or title, cwd=worktree
    )
    if rc == 0:
        return out.strip()
    if "already exists" in err.lower() or "already exists" in out.lower():
        vrc, vout, _ve = await _gh("pr", "view", branch, "--json", "url", "--jq", ".url", cwd=worktree)
        if vrc == 0 and vout.strip():
            url = vout.strip()
            if promote_draft:
                await _promote_adopted_draft(url, branch, cwd=worktree)
            return url
    raise WorktreeError(f"gh pr create failed: {err.strip()[:300]}")


async def _promote_adopted_draft(pr_url: str, branch: str, *, cwd: str) -> None:
    """#207: the "already exists" PR we adopt may be one the CODER opened itself
    (``gh pr create --draft`` from its worktree, before the loop got here). The loop
    owns the PR lifecycle — the coder was told to build and push, not to gate the
    merge — so a draft is not a signal to honour: mark it ready BEFORE the review /
    merge gates run. Otherwise the card walks CI-fix → review-clean normally and then
    parks: GitHub reports ``mergeStateStatus=CLEAN`` for a draft, ``gh pr merge``
    refuses with "pull request is in draft state", and every retry burns an
    ``auto_merge_max`` attempt. Best-effort: an ``isDraft`` read or ``gh pr ready``
    failure logs and proceeds — ``_auto_merge_blockers``' named ``draft`` blocker is
    the backstop. A non-draft is untouched — and ``open_pr`` only calls this on the
    card's FIRST adoption (``promote_draft``), never on a re-dispatch of a card that
    already owns its PR (an operator's draft-as-hold on the loop's own PR stays)."""
    try:
        info = await pr_merge_info(pr_url, cwd=cwd)
    except WorktreeError as exc:
        log.warning("[project_board] %s: could not read isDraft for adopted PR %s: %s", branch, pr_url, exc)
        return
    if info.get("isDraft") is not True:
        return
    try:
        rc, _out, err = await _gh("pr", "ready", pr_url, cwd=cwd)
    except WorktreeError as exc:
        rc, err = 1, str(exc)
    if rc == 0:
        log.info(
            "[project_board] %s adopted the coder's DRAFT PR %s — marked ready (the loop owns the PR lifecycle)",
            branch,
            pr_url,
        )
    else:
        log.warning(
            "[project_board] %s adopted the coder's DRAFT PR %s but `gh pr ready` failed (%s) — "
            "the auto-merge edge will hold on it as a draft; run `gh pr ready %s`",
            branch,
            pr_url,
            (err or "").strip()[:200],
            pr_url,
        )


async def pr_state(pr_url: str, *, cwd: str = ".") -> str:
    """The PR's state — ``MERGED`` / ``CLOSED`` / ``OPEN`` — or ``""`` on a ``gh``
    failure (the next poll just retries; this never raises into the loop). The PR
    reconcile drives the board's Done/closed edges off this (the fallback to the
    webhook for deployments with no public webhook URL)."""
    rc, out, _err = await _gh("pr", "view", pr_url, "--json", "state", "--jq", ".state", cwd=cwd)
    return out.strip() if rc == 0 else ""


async def pr_head_sha(pr_url: str, *, cwd: str = ".") -> str:
    """The PR's current head commit sha (``headRefOid``) — or ``""`` on a ``gh``
    failure (the next poll just retries; this never raises into the loop). The
    review-gate reconcile (#328) reads this to tell whether an external/human push
    moved the head out from under a ``changes-requested`` verdict since the gate last
    reviewed it — the recorded-SHA identity a stale-verdict re-arm turns on."""
    rc, out, _err = await _gh("pr", "view", pr_url, "--json", "headRefOid", "--jq", ".headRefOid", cwd=cwd)
    return out.strip() if rc == 0 else ""


async def pr_merge_info(pr_url: str, *, cwd: str = ".") -> dict:
    """ONE ``gh pr view`` read of the merge-relevant PR facts:
    ``{"mergeStateStatus": str, "isDraft": bool | None}``. ``mergeStateStatus`` is
    ``CLEAN`` / ``BEHIND`` / ``DIRTY`` / ``BLOCKED`` / ``UNSTABLE`` / ``UNKNOWN`` /
    ``DRAFT`` / ``HAS_HOOKS`` — or ``""`` on a gh failure; ``isDraft`` is ``None`` when
    unknown (gh failed / field absent). ``isDraft`` rides the same read (#207) because
    GitHub reports ``CLEAN`` for a draft whose checks pass, so the status alone never
    says "draft" — and ``gh pr merge`` on a draft fails. Never raises into the loop."""
    rc, out, _err = await _gh("pr", "view", pr_url, "--json", "isDraft,mergeStateStatus", cwd=cwd)
    if rc != 0:
        return {"mergeStateStatus": "", "isDraft": None}
    try:
        data = json.loads(out or "{}")
    except ValueError:
        return {"mergeStateStatus": "", "isDraft": None}
    if not isinstance(data, dict):
        return {"mergeStateStatus": "", "isDraft": None}
    draft = data.get("isDraft")
    return {
        "mergeStateStatus": str(data.get("mergeStateStatus") or "").strip(),
        "isDraft": draft if isinstance(draft, bool) else None,
    }


async def pr_merge_state(pr_url: str, *, cwd: str = ".") -> str:
    """The PR's ``mergeStateStatus`` — ``CLEAN`` / ``BEHIND`` / ``DIRTY`` / ``BLOCKED``
    / ``UNSTABLE`` / ``UNKNOWN`` / ``DRAFT`` / ``HAS_HOOKS`` — or ``""`` on a gh
    failure. ``BEHIND`` = stale base, no conflict (a clean rebase fixes it); ``DIRTY``
    = a real conflict with base; ``BLOCKED`` = checks not satisfied (the CI reconcile's
    job, not the rebase's). Never raises into the loop. (The status half of
    ``pr_merge_info`` — the rebase edge only needs this.)"""
    return (await pr_merge_info(pr_url, cwd=cwd))["mergeStateStatus"]


async def merge_pr(pr_url: str, *, method: str = "squash", cwd: str = ".", expected_head: str = "") -> tuple[bool, str]:
    """Merge an open PR via ``gh pr merge`` (the auto-merge edge). ``method`` is
    ``squash`` / ``merge`` / ``rebase``. Returns ``(ok, detail)`` — never raises into
    the loop; a refusal (branch protection, a required review, a race with a
    concurrent merge) is the caller's to log and retry or give up on.

    ``expected_head`` (a commit sha) pins the merge to that head via
    ``gh pr merge --match-head-commit`` (GitHub's ``expectedHeadOid``): if a push
    landed after the caller read/verified the head, GitHub REJECTS the merge atomically
    rather than merging the newer, unreviewed commit. This is the race-free other half
    of the review gate's last-moment head-pin check (#323/#347) — the caller's own
    read-then-compare closes the gate on a stale pin, but only ``--match-head-commit``
    makes the merge itself refuse a head that moved in the window between that read and
    this call. Empty = no constraint (the historical behavior, for the unpinned /
    grandfathered / gate-off paths that have no verified head to pin to).

    Deliberately NOT ``--delete-branch``: gh deletes the LOCAL branch too, and
    ``feat/<fid>`` is checked out in the feature's worktree, so the merge landed and
    then gh exited non-zero on the local delete — a successful merge read as a refusal
    (2026-08-20, bd-p9q/bd-wrl). The remote branch goes via ``delete_remote_branch``
    once the board has read MERGED; the worktree is reaped there too."""
    flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(str(method).lower(), "--squash")
    args = ["pr", "merge", pr_url, flag]
    if expected_head:
        args += ["--match-head-commit", expected_head]
    rc, out, err = await _gh(*args, cwd=cwd, timeout=120)
    detail = (err or out or "").strip()
    return rc == 0, detail


# ``close_pr`` / ``close_pr_sync`` detail values for a PR that needed NO close — the
# caller's bead note must say "already merged", never "close it by hand" on a merged PR.
PR_ALREADY_MERGED = "already merged"
PR_ALREADY_CLOSED = "already closed"
_PR_ALREADY = {"MERGED": PR_ALREADY_MERGED, "CLOSED": PR_ALREADY_CLOSED}


async def close_pr(pr_url: str, *, comment: str, cwd: str = ".") -> tuple[bool, str]:
    """Close an open PR with a comment (``gh pr close --comment``) — the operator-
    cancel edge (#211): a cancelled card must not leave an open PR nobody owns.

    Reads the PR's state FIRST: a ``MERGED`` / ``CLOSED`` PR is left alone and reported
    as ``(True, PR_ALREADY_MERGED | PR_ALREADY_CLOSED)`` — a blind ``gh pr close`` on a
    merged PR fails, and "close it by hand" on merged work is the wrong note. Otherwise
    ``(True, "")`` on a close, ``(False, detail)`` on a gh failure / timeout — never
    raises into the loop."""
    try:
        already = _PR_ALREADY.get(await pr_state(pr_url, cwd=cwd))
        if already:
            return True, already
        rc, out, err = await _gh("pr", "close", pr_url, "--comment", comment, cwd=cwd, timeout=60)
    except Exception as exc:  # noqa: BLE001 — best-effort
        return False, str(exc)
    return rc == 0, "" if rc == 0 else (err or out or "").strip()


def _gh_sync(*args: str, cwd: str, timeout: float) -> tuple[int, str, str]:
    """``_gh`` for a sync caller (a worker thread with no event loop). Raises on a
    missing gh / timeout / bad cwd — the callers wrap it."""
    proc = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def close_pr_sync(pr_url: str, *, comment: str, cwd: str = ".", timeout: float = 60) -> tuple[bool, str]:
    """``close_pr`` for a SYNC caller (the ``board_cancel_feature`` tool runs in a
    worker thread with no event loop of its own). Same contract: ``(ok, detail)``
    with the same ``PR_ALREADY_*`` skip for a merged/closed PR, never raises."""
    try:
        rc, out, _err = _gh_sync("pr", "view", pr_url, "--json", "state", "--jq", ".state", cwd=cwd, timeout=timeout)
        already = _PR_ALREADY.get(out.strip()) if rc == 0 else None
        if already:
            return True, already
        rc, out, err = _gh_sync("pr", "close", pr_url, "--comment", comment, cwd=cwd, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — missing gh, timeout, bad cwd
        return False, str(exc)
    return rc == 0, "" if rc == 0 else (err or out or "").strip()


async def delete_remote_branch(repo: str, branch: str) -> bool:
    """Best-effort ``git push origin --delete <branch>`` — the remote half of the
    merged feature's cleanup. False on any failure (already gone, protected, offline);
    never raises. Touches no local ref, so a worktree holding the branch is fine."""
    try:
        rc, _o, _e = await _git(repo, "push", "origin", "--delete", branch, timeout=60)
    except Exception:  # noqa: BLE001
        return False
    return rc == 0


async def rebase_onto_base(repo: str, branch: str, base: str, *, root: str = ".worktrees") -> tuple[str, str]:
    """Rebase ``origin/<branch>`` onto ``origin/<base>`` in a throwaway DETACHED
    worktree, then force-push the result. Returns:

    - ``("clean", "")``       — rebased + pushed; the PR is fresh against base again
    - ``("conflict", files)`` — the rebase hit conflicts (aborted; remote untouched)
    - ``("error", msg)``      — an infra failure (fetch / worktree / push)

    DETACHED (``origin/<branch>`` at a detached HEAD) so it never collides with the
    feature's own checked-out ``feat-<id>`` worktree — a branch can't be checked out
    twice. The force-push is lease-guarded and the branch is the loop's throwaway."""
    rel = os.path.join(root, f".rebase-{branch.replace('/', '-')}")
    path = os.path.join(repo, rel)
    await _git(repo, "worktree", "remove", "--force", rel)  # clear a stale leftover
    rc, _o, err = await _git(repo, "fetch", "origin", base, branch, timeout=120)
    if rc != 0:
        return ("error", f"fetch failed: {err.strip()[:200]}")
    rc, _o, err = await _git(repo, "worktree", "add", "--detach", "--force", rel, f"origin/{branch}", timeout=60)
    if rc != 0:
        return ("error", f"worktree add failed: {err.strip()[:200]}")
    try:
        rc, out, err = await _git(path, "-c", "rebase.autoStash=false", "rebase", f"origin/{base}", timeout=180)
        if rc != 0:
            _rc, files, _e = await _git(path, "diff", "--name-only", "--diff-filter=U")
            await _git(path, "rebase", "--abort")
            return ("conflict", files.strip() or (out or err).strip()[:300])
        rc, _o, err = await _git(path, "push", "--force-with-lease", "origin", f"HEAD:{branch}", timeout=180)
        if rc != 0:
            return ("error", f"push failed: {err.strip()[:200]}")
        return ("clean", "")
    finally:
        await _git(repo, "worktree", "remove", "--force", rel)


async def origin_head_sha(repo: str, ref: str) -> str:
    """Fetch ``origin/<ref>`` and return its sha — ``""`` on any git failure (the
    caller's next poll retries; never raises into the loop on a plain non-zero
    exit). The merged-state verify (#131) reads this to decide whether base moved
    under an ``in_review`` PR since its verdict was last stamped."""
    rc, _o, _err = await _git(repo, "fetch", "origin", ref, timeout=120)
    if rc != 0:
        return ""
    rc, out, _err = await _git(repo, "rev-parse", f"origin/{ref}")
    return out.strip() if rc == 0 else ""


async def merged_state_worktree(repo: str, branch: str, base_sha: str, *, root: str = ".worktrees") -> tuple[str, str]:
    """Build the MERGED state — ``origin/<branch>`` tip + ``base_sha`` (the base
    commit the verdict will be stamped against, which the caller just fetched via
    ``origin_head_sha`` so it is locally reachable) — in a throwaway DETACHED
    worktree, with NO push: the branch, the PR, and its CI stay untouched (vs
    ``rebase_onto_base``, which force-pushes). Returns:

    - ``("merged", path)``    — the worktree holds the merged tree; the CALLER runs
      the gate there and must ``remove_worktree(repo, path)`` when done
    - ``("conflict", files)`` — the merge hit conflicts (worktree removed; a real
      conflict is the DIRTY/rebase edge's job, not a verdict)
    - ``("error", msg)``      — an infra failure (fetch / worktree add / merge tooling)

    DETACHED for the same reason as the rebase worktree: ``feat-<id>`` already has
    the branch checked out, and a branch can't be checked out twice. The merge
    commit is local scratch, so the committer identity is pinned inline (no reliance
    on the target repo's git config). ``node_modules`` is linked in like
    ``create_worktree`` so an npm/pnpm gate resolves deps instead of false-failing."""
    rel = os.path.join(root, f".verify-{branch.replace('/', '-')}")
    path = os.path.join(repo, rel)
    await _git(repo, "worktree", "remove", "--force", rel)  # clear a stale leftover
    rc, _o, err = await _git(repo, "fetch", "origin", branch, timeout=120)
    if rc != 0:
        return ("error", f"fetch failed: {err.strip()[:200]}")
    rc, _o, err = await _git(repo, "worktree", "add", "--detach", "--force", rel, f"origin/{branch}", timeout=60)
    if rc != 0:
        return ("error", f"worktree add failed: {err.strip()[:200]}")
    abspath = os.path.abspath(path)
    await asyncio.to_thread(link_node_modules, repo, abspath)
    rc, out, err = await _git(
        abspath,
        "-c",
        "user.name=project-board",
        "-c",
        "user.email=project-board@localhost",
        "merge",
        "--no-edit",
        base_sha,
        timeout=180,
    )
    if rc != 0:
        _rc, files, _e = await _git(abspath, "diff", "--name-only", "--diff-filter=U")
        await _git(abspath, "merge", "--abort")
        await _git(repo, "worktree", "remove", "--force", rel)
        return ("conflict", files.strip() or (out or err).strip()[:300])
    return ("merged", abspath)


# Appended when ``pr_diff`` had to cut the diff. Callers that need the WHOLE diff to be
# sound — grounding a review finding's quote against it (#381) — test for this marker and
# decline rather than judging against a fragment: a quote missing from a truncated diff is
# indistinguishable from a quote that was never in the code.
DIFF_TRUNCATED_MARKER = "…(diff truncated)"

# The prompt budget for a carried diff — one source, so a caller re-cutting a diff it
# already fetched cannot drift from `pr_diff`'s own default.
PR_DIFF_MAX_CHARS = 4000


def truncate_diff(text: str, max_chars: int) -> str:
    """``text`` capped at ``max_chars``, marked when it was actually cut."""
    return text if len(text) <= max_chars else text[:max_chars] + f"\n{DIFF_TRUNCATED_MARKER}"


async def pr_diff(pr_url: str, *, cwd: str = ".", max_chars: int = PR_DIFF_MAX_CHARS) -> str:
    """The PR's unified diff, truncated — the prior attempt's actual work, carried
    into the next (escalated) re-dispatch's prompt so a stronger coder FIXES the
    specific code that failed CI instead of re-deriving from scratch (fresh-both
    keeps a fresh session, but the lesson travels). Best-effort: "" on any gh error.

    ``max_chars`` is a PROMPT budget, not a correctness one. Pass a large cap when the
    caller needs the diff to be complete (see ``DIFF_TRUNCATED_MARKER``)."""
    rc, out, _err = await _gh("pr", "diff", pr_url, cwd=cwd)
    if rc != 0 or not out.strip():
        return ""
    return truncate_diff(out.strip(), max_chars)


def _is_blocking_check(c: dict) -> bool:
    """Whether this check's state should gate the feature — i.e. whether a FAILURE
    here is worth bouncing the feature back to the coder.

    Required checks (branch protection) and GitHub Actions runs are blocking; a
    third-party ADVISORY signal — CodeRabbit, coverage bots, the QA panel — is NOT, so
    its red must never burn a coder run on a signal we can't fix (bd-1zp).

    **Advisory does not imply the commit-status API.** The original rule was
    ``__typename != "StatusContext"``, i.e. every check RUN gates. That assumed
    third parties all publish through the legacy status API, and GitHub Apps do not:
    an App publishes through the Checks API, so its advisory verdict arrives as a
    ``CheckRun`` and gated the board. That is how the ``QA panel`` run from the
    ``protoreview`` App — not required on ``main``, and unfixable by a coder because it
    reports UNRESOLVED REVIEW THREADS rather than anything in the diff — bounced cards
    into escalation and terminal blocks (bd-zrfv burned 5 attempts, bd-sldt 2).

    The discriminator is ``workflowName``: a GitHub Actions run always carries the
    workflow it came from, and an App's check run carries an empty one. Verified against
    a live rollup — all 20 Actions runs populated it; the App's ``QA panel`` was the lone
    ``""``.

    ``isRequired`` still overrides everything: a check the repo marks required gates
    whatever published it. (Note ``gh`` frequently reports ``isRequired: null`` even for
    genuinely required checks, so it can promote but never demote.)

    Conservative default: any shape we do not positively recognise — an older ``gh`` that
    omits ``__typename`` or ``workflowName`` — stays blocking, so a real failure is never
    silently dropped."""
    if c.get("isRequired") is True:
        return True
    typename = str(c.get("__typename") or "")
    if typename == "StatusContext":
        return False
    # Only demote a check run we can positively identify as non-Actions: the key must be
    # present AND empty. A missing key means an older `gh` we cannot judge — stay blocking.
    if typename == "CheckRun" and "workflowName" in c:
        return bool(str(c.get("workflowName") or "").strip())
    return True


async def pr_ci_status(pr_url: str, *, cwd: str = ".", log_chars: int = 3000) -> tuple[str, str]:
    """The PR's CI rollup → ``("passing" | "failing" | "pending" | "none", summary)``.

    The closed-loop verify edge: the reconcile poll uses this to bounce a feature
    whose checks FAILED back to the coder with the failure as feedback (vs the old
    behavior — a red PR sat in_review forever). Best-effort: any ``gh`` failure
    returns ``("none", "")`` so the caller just leaves the PR alone (never raises
    into the loop). For a failing rollup, ``summary`` names the failing checks and,
    best-effort, includes a truncated excerpt of the first failing run's log so the
    coder can actually fix it (edit-only — it can't re-run the checks itself).

    Only BLOCKING checks (required checks + GitHub Actions runs, see
    ``_is_blocking_check``) decide the rollup. A red third-party ADVISORY status
    (CodeRabbit, a coverage bot) is ignored — it can't gate the merge, so it must not
    trigger a CI-bounce; the rollup reads ``passing`` when every blocking check is green
    even while an advisory one is red (bd-1zp)."""
    rc, out, _err = await _gh(
        "pr", "view", pr_url, "--json", "statusCheckRollup", "--jq", ".statusCheckRollup", cwd=cwd
    )
    if rc != 0 or not out.strip():
        return "none", ""
    try:
        checks = json.loads(out) or []
    except json.JSONDecodeError:
        return "none", ""
    if not checks:
        return "none", ""

    _FAIL = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
    _PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED", ""}

    def _conclusion(c: dict) -> str:
        # GH Actions checks carry `conclusion` (+ `status` while running); legacy
        # status contexts carry `state`. Normalize to an upper-case token.
        return str(c.get("conclusion") or c.get("status") or c.get("state") or "").upper()

    def _name(c: dict) -> str:
        return str(c.get("name") or c.get("context") or c.get("workflowName") or "check")

    # Only checks that actually gate the merge count — a red advisory status is dropped
    # here so it can neither read as `failing` nor hold the rollup `pending`.
    gating = [c for c in checks if _is_blocking_check(c)]
    failing = [c for c in gating if _conclusion(c) in _FAIL]
    if not failing:
        pending = [c for c in gating if _conclusion(c) in _PENDING and _conclusion(c) != "SUCCESS"]
        # SUCCESS/NEUTRAL/SKIPPED all count as not-blocking → passing once nothing pends.
        return ("pending", "") if pending else ("passing", "")

    lines = [f"- {_name(c)}: {_conclusion(c)}" for c in failing]
    summary = "Failing checks:\n" + "\n".join(lines)
    # Best-effort: pull the first failing GH-Actions run's failed-step log so the
    # coder sees the actual error, not just the check name.
    detail_url = next((str(c.get("detailsUrl") or "") for c in failing if c.get("detailsUrl")), "")
    run_id = ""
    if "/actions/runs/" in detail_url:
        run_id = detail_url.split("/actions/runs/", 1)[1].split("/", 1)[0]
    if run_id.isdigit():
        lrc, lout, _le = await _gh("run", "view", run_id, "--log-failed", cwd=cwd, timeout=60)
        if lrc == 0 and lout.strip():
            summary += f"\n\nFailing log (truncated):\n{lout.strip()[-log_chars:]}"
    return "failing", summary


# ── #347's App-only check-run seam removed (bd-doo0) ─────────────────────────────────
# #347 published the review-gate verdict as a GitHub CHECK RUN (post_review_check /
# read_review_check / _existing_review_check_id). ``POST /check-runs`` requires a GitHub App
# INSTALLATION token, so under the board's user/PAT ``gh`` credential that path ALWAYS 403s
# ("You must authenticate via a GitHub App") — no token scope can make it succeed. #354 moved
# publication + readback to the PAT-compatible COMMIT STATUS seam below; the dead check-run
# helpers (and their mock-only tests) are removed so nothing can reintroduce a publisher that
# is structurally unavailable under the deployed credential model. ``REVIEW_STATUS_CONTEXT``
# below keeps the historical ``QA panel`` name, so the PR still shows ONE coherent QA signal.


# ── #354: PAT-compatible commit-status publication of the review-gate verdict ─────────
# #347 published the verdict as a GitHub CHECK RUN, but ``POST /check-runs`` requires a
# GitHub App INSTALLATION token: under the board's user/PAT ``gh`` credential it ALWAYS
# 403s ("You must authenticate via a GitHub App"), so that path is structurally inert here
# — no token scope can make it succeed. A COMMIT STATUS (``POST /repos/{repo}/statuses/{sha}``)
# is the PAT-compatible signal: a user/PAT token with ``repo`` scope can create it, it is
# pinned to an immutable commit exactly like a check run, and it surfaces on the PR as the
# same merge-relevant rollup. The context keeps the historical ``QA panel`` name so the PR
# shows ONE coherent QA signal (the same context the review workflow has posted as a side
# effect), and ``read_review_status`` reconciles that single record rather than a parallel one.
REVIEW_STATUS_CONTEXT = "QA panel"

# GitHub commit-status ``state`` values (the merge-relevant ones the gate uses). An unknown
# value is coerced to ``error`` so a caller typo can never make gh reject the POST and drop
# the verdict entirely (the check-run publisher's ``neutral`` coercion, one layer down).
_STATUS_STATES = frozenset({"success", "failure", "error", "pending"})
# GitHub caps a commit status ``description`` at 140 chars; truncate so a long verdict line
# never makes the whole post fail. The full findings ride the PR comment, not the status.
_STATUS_DESCRIPTION_MAX = 140

# The one board-authored PR comment carrying the blocking verdict's full findings (#354) is
# tagged with this hidden marker, so a re-post UPDATES it in place instead of stacking a new
# comment every reconcile tick (idempotency, r4). One marked comment per PR.
REVIEW_COMMENT_MARKER = "<!-- project-board:review-gate -->"

_PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")


def _parse_pr(pr_url: str) -> tuple[str, str]:
    """``(repo_slug, number)`` from a GitHub PR url, or ``("", "")``. worktree.py's own
    parser — ``loop._parse_pr_url`` lives one layer up (the loop imports worktree, not the
    reverse), so the PR-comment/status helpers here can't borrow it."""
    m = _PR_URL_RE.search(pr_url or "")
    return (m.group(1), m.group(2)) if m else ("", "")


async def post_review_status(
    repo_slug: str,
    head_sha: str,
    *,
    state: str,
    description: str,
    target_url: str = "",
    context: str = REVIEW_STATUS_CONTEXT,
    cwd: str = ".",
) -> bool:
    """Publish the in-loop review gate's verdict as a PAT-compatible COMMIT STATUS pinned to
    ``head_sha`` (#354) — ``POST /repos/{slug}/statuses/{sha}``. Unlike #347's check run
    (which needs a GitHub App token and 403s under the board's user/PAT credential), a commit
    status is creatable by any token carrying ``repo``/``statuses:write`` scope, so this is
    the endpoint that actually lands the verdict where the PR is reviewed. Returns True on a
    landed POST, False otherwise; NEVER raises into the loop (the bead comment stays the
    durable audit record).

    Head-safe (#328): an empty ``repo_slug`` or ``head_sha`` means the head the gate reviewed
    is unknown, so NOTHING is posted — a verdict must never land against a head the gate did
    not examine. That missing-head skip is the CALLER's to log (distinct from a permission/API
    refusal, #354 r7); this returns False without shelling gh. ``state`` is one of
    ``_STATUS_STATES`` (an unknown value degrades to ``error``); ``description`` is truncated
    to GitHub's 140-char cap; a ``target_url`` (the stable PR link) is attached when given.

    Idempotent by construction (r4): a commit status is keyed by ``(context, sha)`` — re-posting
    the same context on the same commit SUPERSEDES the prior state in the PR's combined rollup,
    so a reconcile/retry of the same verdict reconciles the single ``QA panel`` signal rather
    than stacking duplicate records."""
    if not repo_slug or not head_sha:
        return False
    if state not in _STATUS_STATES:
        state = "error"
    description = (description or "")[:_STATUS_DESCRIPTION_MAX]
    fields = ["-f", f"state={state}", "-f", f"context={context}", "-f", f"description={description}"]
    if target_url:
        fields += ["-f", f"target_url={target_url}"]
    args = ["api", "--method", "POST", f"/repos/{repo_slug}/statuses/{head_sha}", *fields]
    try:
        rc, _out, err = await _gh(*args, cwd=cwd)
    except WorktreeError as exc:
        log.warning("[project_board] review status post timed out for %s@%s: %s", repo_slug, head_sha[:12], exc)
        return False
    if rc != 0:
        # A publication PERMISSION/API refusal (no `statuses:write`, an App-only token, a
        # network blip) — logged here, distinct from the caller's missing-head skip (#354 r7).
        log.warning(
            "[project_board] review status post failed (%s@%s): %s",
            repo_slug,
            head_sha[:12],
            (err or "").strip()[:200],
        )
        return False
    return True


async def read_review_status(
    repo_slug: str,
    head_sha: str,
    *,
    context: str = REVIEW_STATUS_CONTEXT,
    cwd: str = ".",
) -> dict | None:
    """Read back the head-pinned ``QA panel`` COMMIT STATUS (#354, the PAT-compatible successor
    to #347's ``read_review_check``) — the inbound identity plumbing #323's trusted-verdict
    reconcile turns on. Returns the promoted verdict recorded for ``head_sha`` as
    ``{"state": str, "head_sha": str, "passed": bool}``, or ``None`` when no trusted verdict can
    be PROVEN for that exact head.

    Reads the COMBINED status of the commit (``GET /repos/{slug}/commits/{sha}/status``, jq'd to
    ``.statuses``): GitHub collapses that to ONE latest status per context, so a ``QA panel``
    match is unambiguous by construction — there is never a stale earlier ``QA panel`` state
    racing the current one. The endpoint is head-scoped by the URL, so a status it returns IS
    for ``head_sha`` (the currency invariant of #328). ``passed`` is ``state == "success"`` — a
    green CI rollup and an unpinned review comment are NOT this signal; only the named status is.

    Fails CLOSED to ``None`` — so a caller can never act on a signal it could not read cleanly,
    and never promotes from ambiguous/untrusted status data (#354 r5) — on an empty slug/head, a
    ``gh`` error, malformed/non-list JSON, NO ``QA panel`` status, or (the defensive case the
    combined endpoint should preclude) MORE THAN ONE match. A completed NON-success state returns
    ``passed=False`` — a real, trusted verdict a caller must not promote off, DISTINCT from an
    unreadable ``None``. Never raises into the loop."""
    if not repo_slug or not head_sha:
        return None
    try:
        rc, out, _err = await _gh("api", f"/repos/{repo_slug}/commits/{head_sha}/status", "--jq", ".statuses", cwd=cwd)
    except WorktreeError:
        return None
    if rc != 0 or not out.strip():
        return None
    try:
        statuses = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(statuses, list):
        return None
    matched = [s for s in statuses if isinstance(s, dict) and s.get("context") == context]
    if len(matched) != 1:
        return None  # absent, or (defensively) ambiguous → fail closed
    state = matched[0].get("state")
    if not isinstance(state, str) or not state:
        return None  # malformed → fail closed
    return {"state": state, "head_sha": head_sha, "passed": state == "success"}


async def _find_marked_comment(repo_slug: str, number: str, marker: str, *, cwd: str) -> tuple[str, str]:
    """The ``(id, body)`` of the board's marked PR comment (the one whose body contains
    ``marker``), or ``("", "")`` when none exists or the list can't be read — the caller then
    CREATEs. Best-effort; never raises. ``--paginate`` so a long comment thread doesn't hide
    the marked comment past the first page and cause a duplicate post."""
    try:
        rc, out, _err = await _gh("api", "--paginate", f"/repos/{repo_slug}/issues/{number}/comments", cwd=cwd)
    except WorktreeError:
        return "", ""
    if rc != 0 or not out.strip():
        return "", ""
    try:
        comments = json.loads(out)
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(comments, list):
        return "", ""
    for c in comments:
        if isinstance(c, dict) and marker in str(c.get("body") or ""):
            return str(c.get("id") or ""), str(c.get("body") or "")
    return "", ""


async def post_or_update_pr_comment(
    pr_url: str, body: str, *, marker: str = REVIEW_COMMENT_MARKER, cwd: str = "."
) -> bool:
    """Post — or idempotently UPDATE — a single board-authored PR comment identified by a hidden
    HTML ``marker`` (#354). The blocking review-gate verdict's full actionable findings go here
    so a human sees the rationale GitHub-side, while the bead comment stays the board's audit
    record. Returns True on a landed create/update (or an idempotent no-op), False otherwise;
    never raises into the loop.

    Idempotent per PR (r4): the existing marked comment is found and PATCHed in place, so a
    reconcile tick that re-renders the SAME findings does not spam a duplicate — and if the
    rendered body is byte-identical to what is already there, it is a no-op (no needless PATCH
    churn, no "misleading repeated record"). The marker is prepended to the posted body so the
    found/desired comparison is apples-to-apples. An unparseable url (no PR number / repo slug)
    posts nothing → False."""
    repo_slug, number = _parse_pr(pr_url)
    if not repo_slug or not number:
        return False
    full = body if body.startswith(marker) else f"{marker}\n{body}"
    existing_id, existing_body = await _find_marked_comment(repo_slug, number, marker, cwd=cwd)
    if existing_id and existing_body == full:
        return True  # already exactly this comment — idempotent no-op, no PATCH
    if existing_id:
        args = ["api", "--method", "PATCH", f"/repos/{repo_slug}/issues/comments/{existing_id}", "-f", f"body={full}"]
    else:
        args = ["api", "--method", "POST", f"/repos/{repo_slug}/issues/{number}/comments", "-f", f"body={full}"]
    try:
        rc, _out, err = await _gh(*args, cwd=cwd)
    except WorktreeError as exc:
        log.warning("[project_board] review PR comment post timed out for %s#%s: %s", repo_slug, number, exc)
        return False
    if rc != 0:
        log.warning(
            "[project_board] review PR comment post failed (%s#%s): %s",
            repo_slug,
            number,
            (err or "").strip()[:200],
        )
        return False
    return True


async def pr_url_for_branch(branch: str, *, cwd: str = ".") -> str:
    """The URL of the PR whose head is ``branch``, or ``""`` if there is none — used
    by crash recovery to tell a feature that already opened a PR (and just needs
    adopting → in_review) from one that needs a fresh rebuild."""
    rc, out, _err = await _gh("pr", "view", branch, "--json", "url", "--jq", ".url", cwd=cwd)
    return out.strip() if rc == 0 else ""


async def repo_slug(*, cwd: str = ".") -> str:
    """The ``owner/name`` slug of the checkout's default GitHub repo — the repo a PR
    opened from here TARGETS — or ``""`` when it can't be resolved.

    Fails OPEN: a ``gh`` non-zero exit OR a ``WorktreeError`` (the timeout ``_gh``
    raises) returns ``""`` instead of propagating, so a caller (e.g. the PR-body
    source-issue stamp) that can't learn the target repo simply degrades rather than
    blocking the PR. This never raises into the loop."""
    try:
        rc, out, _err = await _gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner", cwd=cwd)
    except WorktreeError:
        return ""
    return out.strip() if rc == 0 else ""
