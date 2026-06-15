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
    reaped here, not left to pile up.

    Fresh-both: every attempt gets a freshly recreated worktree (``create_worktree``
    wipes + rebuilds it off the base), so the coder must also start a FRESH ACP
    session. Otherwise a re-dispatch (CI-fail bounce, tier escalation, crash
    recovery) would ``session/load``-resume a thread whose memory references a diff
    the wiped tree no longer has — the coder thinks it's already done (→ no diff) or
    edits against stale assumptions. Forgetting the session first keeps its memory in
    step with the empty tree. (A first attempt has no session to forget → no-op.)"""
    from plugins.delegates.adapters import ADAPTERS, DelegateError

    adapter = ADAPTERS["acp"]
    scoped = dataclasses.replace(coder, workdir=worktree)
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
            return vout.strip()
    raise WorktreeError(f"gh pr create failed: {err.strip()[:300]}")


async def pr_state(pr_url: str, *, cwd: str = ".") -> str:
    """The PR's state — ``MERGED`` / ``CLOSED`` / ``OPEN`` — or ``""`` on a ``gh``
    failure (the next poll just retries; this never raises into the loop). The PR
    reconcile drives the board's Done/closed edges off this (the fallback to the
    webhook for deployments with no public webhook URL)."""
    rc, out, _err = await _gh("pr", "view", pr_url, "--json", "state", "--jq", ".state", cwd=cwd)
    return out.strip() if rc == 0 else ""


async def pr_ci_status(pr_url: str, *, cwd: str = ".", log_chars: int = 3000) -> tuple[str, str]:
    """The PR's CI rollup → ``("passing" | "failing" | "pending" | "none", summary)``.

    The closed-loop verify edge: the reconcile poll uses this to bounce a feature
    whose checks FAILED back to the coder with the failure as feedback (vs the old
    behavior — a red PR sat in_review forever). Best-effort: any ``gh`` failure
    returns ``("none", "")`` so the caller just leaves the PR alone (never raises
    into the loop). For a failing rollup, ``summary`` names the failing checks and,
    best-effort, includes a truncated excerpt of the first failing run's log so the
    coder can actually fix it (edit-only — it can't re-run the checks itself)."""
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

    failing = [c for c in checks if _conclusion(c) in _FAIL]
    if not failing:
        pending = [c for c in checks if _conclusion(c) in _PENDING and _conclusion(c) != "SUCCESS"]
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


async def pr_url_for_branch(branch: str, *, cwd: str = ".") -> str:
    """The URL of the PR whose head is ``branch``, or ``""`` if there is none — used
    by crash recovery to tell a feature that already opened a PR (and just needs
    adopting → in_review) from one that needs a fresh rebuild."""
    rc, out, _err = await _gh("pr", "view", branch, "--json", "url", "--jq", ".url", cwd=cwd)
    return out.strip() if rc == 0 else ""
