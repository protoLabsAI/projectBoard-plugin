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
import subprocess
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
    except asyncio.CancelledError:
        # #211: a task cancel (the operator's cancel verb) while the child runs —
        # wait_for re-raises it but does NOT kill the child, so a `git push` would
        # finish anyway and the branch land on the remote. Kill, then propagate.
        _kill_quietly(proc)
        raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _kill_quietly(proc) -> None:
    """``proc.kill()`` that tolerates a child that already exited."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass


# Paths the coder writes as its OWN session scratch — the ACP/`proto` coder's private
# state (`.proto/`: session notes + memory) and editor caches (`.cursor`) — into the
# per-feature worktree (its cwd). They must never ride into the feature PR: they make the
# reviewer-facing diff noisy and leak the agent's internal session notes into the target
# repo's history (#49). ``stage_all`` excludes them so a plain ``add -A`` skips them.
CODER_SCRATCH = (".proto", ".cursor")


async def stage_all(worktree: str) -> tuple[int, str, str]:
    """``git add -A`` over the worktree, MINUS the coder's own scratch (``CODER_SCRATCH``).

    The single staging seam — shared by the commit path and the verify/judge diff probes
    — so all three see the same intended-only file set. Excludes scratch via a pathspec
    (``:(exclude)…``) rather than ``.git/info/exclude``, so it mutates nothing in the repo
    and depends on no target-repo ``.gitignore`` entry: the exclusion is scoped to this one
    staging call. The leading ``.`` is the positive pathspec the excludes subtract from."""
    excludes = [f":(exclude){p}" for p in CODER_SCRATCH]
    return await _git(worktree, "add", "-A", "--", ".", *excludes)


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
    same name first (idempotent re-run after a crashed feature).

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


async def reap_feature_worktree(repo: str, worktrees_root: str, fid: str) -> bool:
    """Remove the worktree(s) + branch(es) a feature owns, by its id — the one place
    that knows the ``feat-<id>`` / ``feat/<id>`` naming. Shared by the merge webhook,
    the merge poll (both reap once a feature reaches ``done``), and the cancel path.

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
    with its matching ``feat/<id>-<slug>`` branch. A candidate uses a ``.`` separator, so
    the ``-`` slug scan can never mistake one for a canonical tree."""
    base = os.path.join(repo, worktrees_root)
    canonical = os.path.join(base, f"feat-{fid}")
    had_canonical = os.path.isdir(canonical)
    removed = await remove_worktree(repo, canonical, f"feat/{fid}")
    cleaned: list[str] = [f"feat-{fid}"] if (removed and had_canonical) else []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        names = []
    # The slugged canonical variant(s): `feat-<fid>-<slug>` (a hyphen tail — a candidate
    # uses a dot). Branch mirrors the dir name (`feat-` → `feat/`).
    for name in names:
        if not name.startswith(f"feat-{fid}-") or not os.path.isdir(os.path.join(base, name)):
            continue
        had_canonical = True
        if await remove_worktree(repo, os.path.join(base, name), "feat/" + name[len("feat-") :]):
            cleaned.append(name)
        else:
            removed = False
    reaped: list[str] = []
    for name in names:
        wt_id = name[len("feat-") :]
        # Only THIS feature's candidates: `feat-<fid>.<suffix>` whose suffixes strip
        # back to fid (handles the stacked `.test.g2` shape; skips `feat-<fid>x`
        # prefix-collisions and non-candidate dots like `.gx`).
        if not name.startswith(f"feat-{fid}.") or parent_feature_id(wt_id) != fid:
            continue
        if not os.path.isdir(os.path.join(base, name)):
            continue
        if await remove_worktree(repo, os.path.join(base, name), f"feat/{wt_id}"):
            reaped.append(name)
    if cleaned or reaped:
        log.info("[project_board] reaped worktrees for %s: %s", fid, ", ".join(cleaned + reaped))
    return removed


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
    first so ``move`` has a free destination. A winner already at the canonical path is
    a no-op. Returns (canonical_path, canonical_branch)."""
    canon_branch = branch_name(fid, title)
    canon_rel = os.path.join(root, worktree_dir(fid, title))
    canon_path = os.path.join(repo, canon_rel)
    if os.path.abspath(src_wt) == os.path.abspath(canon_path):
        return os.path.abspath(canon_path), canon_branch
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
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise WorktreeError(f"gh {' '.join(args)} timed out after {timeout}s")
    except asyncio.CancelledError:
        # #211: same as _git — a cancel mid-`gh pr create` must not let the child
        # finish and open a PR nobody owns. (If it already did, the drive's cancel
        # path finds it by branch — pr_url_for_branch — and closes it.)
        _kill_quietly(proc)
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


async def merge_pr(pr_url: str, *, method: str = "squash", cwd: str = ".") -> tuple[bool, str]:
    """Merge an open PR via ``gh pr merge`` (the auto-merge edge). ``method`` is
    ``squash`` / ``merge`` / ``rebase``. Returns ``(ok, detail)`` — never raises into
    the loop; a refusal (branch protection, a required review, a race with a
    concurrent merge) is the caller's to log and retry or give up on.

    Deliberately NOT ``--delete-branch``: gh deletes the LOCAL branch too, and
    ``feat/<fid>`` is checked out in the feature's worktree, so the merge landed and
    then gh exited non-zero on the local delete — a successful merge read as a refusal
    (2026-08-20, bd-p9q/bd-wrl). The remote branch goes via ``delete_remote_branch``
    once the board has read MERGED; the worktree is reaped there too."""
    flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(str(method).lower(), "--squash")
    rc, out, err = await _gh("pr", "merge", pr_url, flag, cwd=cwd, timeout=120)
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


async def pr_diff(pr_url: str, *, cwd: str = ".", max_chars: int = 4000) -> str:
    """The PR's unified diff, truncated — the prior attempt's actual work, carried
    into the next (escalated) re-dispatch's prompt so a stronger coder FIXES the
    specific code that failed CI instead of re-deriving from scratch (fresh-both
    keeps a fresh session, but the lesson travels). Best-effort: "" on any gh error."""
    rc, out, _err = await _gh("pr", "diff", pr_url, cwd=cwd)
    if rc != 0 or not out.strip():
        return ""
    out = out.strip()
    return out if len(out) <= max_chars else out[:max_chars] + "\n…(diff truncated)"


def _is_blocking_check(c: dict) -> bool:
    """Whether this check's state should gate the feature — i.e. whether a FAILURE
    here is worth bouncing the feature back to the coder.

    Required checks (branch protection) and GitHub Actions runs are blocking; a
    third-party ADVISORY status — CodeRabbit, coverage bots, etc., which post through
    the legacy commit-status API and so arrive as a ``StatusContext`` — is NOT, so its
    red must never burn a coder run on a signal we can't fix (bd-1zp). ``isRequired``
    (when ``gh`` surfaces it) overrides the type: a status the repo marks required is
    always blocking. Conservative default: an unknown shape (an older ``gh`` that omits
    ``__typename``) is treated as blocking so a real failure is never silently dropped."""
    if c.get("isRequired") is True:
        return True
    return str(c.get("__typename") or "") != "StatusContext"


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
