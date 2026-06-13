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
import logging
import os

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


async def _git(repo: str, *args: str, timeout: float = 60) -> tuple[int, str, str]:
    """Run a git command in ``repo``; return (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        repo,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise WorktreeError(f"git {' '.join(args)} timed out after {timeout}s")
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def create_worktree(repo: str, base: str, fid: str, root: str = ".worktrees") -> tuple[str, str]:
    """``git worktree add <root>/feat-<id> -b feat/<id> <base>``.

    Returns (absolute worktree path, branch). The branch is fresh off ``base`` so
    the blast radius is one throwaway tree. Cleans a stale worktree/branch of the
    same name first (idempotent re-run after a crashed feature)."""
    branch = f"feat/{fid}"
    rel = os.path.join(root, f"feat-{fid}")
    path = os.path.join(repo, rel)
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
    rc_chk, _o, _e = await _git(repo, "rev-parse", "--verify", "--quiet", start)
    if rc_chk != 0:
        start = base
    rc, _out, err = await _git(repo, "worktree", "add", rel, "-b", branch, start)
    if rc != 0:
        raise WorktreeError(f"worktree add failed: {err.strip()[:300]}")
    return os.path.abspath(path), branch


async def remove_worktree(repo: str, worktree: str, branch: str = "") -> None:
    """Tear down the worktree (and its branch, once merged the branch is junk).
    Best-effort — teardown must not raise into the loop's success path."""
    rc, _out, err = await _git(repo, "worktree", "remove", "--force", worktree)
    if rc != 0:
        log.warning("[project_board] worktree remove %s failed: %s", worktree, err.strip()[:200])
    if branch:
        await _git(repo, "branch", "-D", branch)


async def reap_feature_worktree(repo: str, worktrees_root: str, fid: str) -> None:
    """Remove the worktree + branch a feature owns, by its id — the one place that
    knows the ``feat-<id>`` / ``feat/<id>`` naming. Shared by the merge webhook and
    the merge poll (both reap once a feature reaches ``done``)."""
    wt = os.path.join(repo, worktrees_root, f"feat-{fid}")
    await remove_worktree(repo, wt, f"feat/{fid}")


def list_feature_worktrees(repo: str, worktrees_root: str) -> list[str]:
    """The feature ids that currently have a ``feat-<id>`` worktree dir under
    ``<repo>/<worktrees_root>`` — for the health sweep's orphan check. Sync (a quick
    dir listing); returns ``[]`` if the dir is absent."""
    base = os.path.join(repo, worktrees_root)
    try:
        names = os.listdir(base)
    except OSError:
        return []
    return [n[len("feat-") :] for n in names if n.startswith("feat-") and os.path.isdir(os.path.join(base, n))]


async def dispatch_coder(coder, worktree: str, prompt: str, *, timeout: float | None = None) -> str:
    """Dispatch the coder (an ``acp`` Delegate) scoped to ``worktree``.

    Builds a per-feature copy with the worktree as workdir (registry untouched),
    dispatches via the adapter, and ALWAYS tears the ACP subprocess down — the
    cache keys on workdir, so each feature owns a distinct client that must be
    reaped here, not left to pile up."""
    from plugins.delegates.adapters import ADAPTERS, DelegateError

    adapter = ADAPTERS["acp"]
    scoped = dataclasses.replace(coder, workdir=worktree)
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
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise WorktreeError(f"gh {' '.join(args)} timed out after {timeout}s")
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def commit_worktree(worktree: str, message: str) -> None:
    """Commit whatever the coder left uncommitted in the worktree. No-op if the
    tree is clean (the coder may have committed its own work)."""
    _rc, out, _err = await _git(worktree, "status", "--porcelain")
    if not out.strip():
        return
    await _git(worktree, "add", "-A")
    rc, o, e = await _git(worktree, "commit", "-m", message)
    if rc != 0 and "nothing to commit" not in (o + e).lower():
        raise WorktreeError(f"commit failed: {(e or o).strip()[:200]}")


async def open_pr(worktree: str, branch: str, *, base: str = "main", title: str, body: str = "") -> str:
    """Commit + push the worktree's branch and open (or reuse) a PR; return its URL.

    Operates **inside the worktree** (the confinement boundary). Raises
    ``NoChangesError`` if the coder produced nothing (no commits vs ``base``) — the
    loop escalates that, vs a push/`gh` failure which it treats as infra → Blocked.
    Idempotent: if a PR already exists for the branch (a re-dispatch after CI fail),
    it pushes the new commits and returns the existing PR url instead of erroring."""
    # 1. Commit anything left uncommitted, then guard against an empty result.
    await commit_worktree(worktree, title)
    _rc, out, _err = await _git(worktree, "rev-list", "--count", f"{base}..HEAD")
    n = int(out.strip()) if out.strip().isdigit() else 0
    if n == 0:
        raise NoChangesError("coder produced no commits vs base — nothing to PR")

    # 2. Push the branch from the worktree.
    rc, _o, err = await _git(worktree, "push", "-u", "origin", branch, timeout=180)
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
            return vout.strip()
    raise WorktreeError(f"gh pr create failed: {err.strip()[:300]}")


async def pr_is_merged(pr_url: str, *, cwd: str = ".") -> bool:
    """True iff the PR has merged — the merge poll's probe (a fallback to the
    webhook for deployments with no public webhook URL). A non-zero ``gh`` /
    transient failure returns False so the next poll simply retries; this never
    raises into the loop."""
    rc, out, _err = await _gh("pr", "view", pr_url, "--json", "state", "--jq", ".state", cwd=cwd)
    return rc == 0 and out.strip() == "MERGED"


async def pr_url_for_branch(branch: str, *, cwd: str = ".") -> str:
    """The URL of the PR whose head is ``branch``, or ``""`` if there is none — used
    by crash recovery to tell a feature that already opened a PR (and just needs
    adopting → in_review) from one that needs a fresh rebuild."""
    rc, out, _err = await _gh("pr", "view", branch, "--json", "url", "--jq", ".url", cwd=cwd)
    return out.strip() if rc == 0 else ""
