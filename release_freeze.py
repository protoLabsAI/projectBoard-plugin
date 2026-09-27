"""Release-freeze guard for the auto-merge edge.

Some repos freeze ``main`` while a release is being prepared: protoAgent's
``prepare-release.yml`` pushes a ``prepare-release/vX.Y.Z`` branch and opens a PR from
it, and every merge to ``main`` during that window restarts the release's checks
(~15 min). The board used to merge straight through it. Now, immediately before the
auto-merge edge would merge a PR, it asks the PR's repo whether a release is in flight,
and HOLDS the merge (the card stays ``in_review``, reading ``held: release freeze
(<evidence>)``) until the freeze lifts. Every merge poll re-checks.

Config — ``release_freeze``, per project entry (falling back to the flat top-level key):

* absent / ``true`` — the DEFAULT patterns: a remote branch or an open PR head matching
  ``prepare-release*``, or an active run of ``prepare-release.yml``. A repo with none of
  those is never frozen, so the default is safe to leave on everywhere (it costs three
  reads per otherwise-ready merge).
* ``false`` — off for that project. Recommended for a changesets repo (protoContent):
  its release is a bot-maintained "Version Packages" PR (``changeset-release/main``)
  that is open whenever ANY changeset is pending, i.e. most of the time; merging other
  PRs meanwhile just folds their changesets into it. Freezing on it would hold nearly
  every merge for nothing.
* a list — shorthand: each item is a glob matched against remote branches AND open PR
  heads, except ``workflow:<file>`` items (or items ending ``.yml``/``.yaml``), which
  name workflows whose active runs freeze.
* a mapping ``{branches: [...], pr_heads: [...], workflows: [...]}`` — each signal
  configured separately (an absent key = that signal off). Use it for a repo that KEEPS
  its release branches after merging (then a branch glob would freeze forever): check
  ``pr_heads`` and ``workflows`` only.

Fail CLOSED: when the check itself errors (GitHub down, rate-limited, no auth) the merge
is held with the error as evidence and retried next poll. A delayed merge costs one poll
interval; a merge into a release in flight costs the release's whole check run.
"""

from __future__ import annotations

import sys
import threading
import time
import types

DEFAULT_PATTERNS = {
    "branches": ["prepare-release*"],
    "pr_heads": ["prepare-release*"],
    "workflows": ["prepare-release.yml"],
}
# Re-reading the same repo's freeze state within this window reuses the answer, so a
# poll that finds five mergeable cards in one repo asks GitHub once, not five times.
CHECK_TTL_S = 30.0


def _as_list(value) -> list[str]:
    if value is None or value is False:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in value if str(v).strip()]


def parse_config(raw) -> dict | None:
    """``release_freeze`` → ``{branches, pr_heads, workflows}``, or None when disabled."""
    if raw is None or raw is True:
        return {k: list(v) for k, v in DEFAULT_PATTERNS.items()}
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("", "true", "on", "yes", "1", "default"):
            return {k: list(v) for k, v in DEFAULT_PATTERNS.items()}
        if low in ("false", "off", "no", "0", "none"):
            return None
        raw = raw.split(",")
    if raw is False:
        return None
    if isinstance(raw, dict):
        out = {
            "branches": _as_list(raw.get("branches")),
            "pr_heads": _as_list(raw.get("pr_heads")),
            "workflows": _as_list(raw.get("workflows")),
        }
        return out if any(out.values()) else None
    globs, workflows = [], []
    for item in _as_list(raw):
        if item.lower().startswith("workflow:"):
            workflows.append(item.split(":", 1)[1].strip())
        elif item.lower().endswith((".yml", ".yaml")):
            workflows.append(item)
        else:
            globs.append(item)
    out = {"branches": list(globs), "pr_heads": list(globs), "workflows": workflows}
    return out if any(out.values()) else None


# ── process-stable state: the per-card holds the listing reads, the per-repo cache ──
_SLOT = "project_board.release_freeze::" + (__name__.rsplit(".", 1)[0] if "." in __name__ else __name__)
_holder = sys.modules.get(_SLOT)
if _holder is None:
    _holder = types.ModuleType(_SLOT)
    _holder.holds = {}
    _holder.checks = {}
    _holder.lock = threading.Lock()
    sys.modules[_SLOT] = _holder
_HOLDS: dict[str, dict] = _holder.holds
_CHECKS: dict[tuple, dict] = _holder.checks
_LOCK: threading.Lock = _holder.lock


def reset_state() -> None:
    with _LOCK:
        _HOLDS.clear()
        _CHECKS.clear()


def hold_for(fid: str) -> dict | None:
    """The live freeze hold on ``fid`` (``{evidence, since, repo}``), or None."""
    with _LOCK:
        h = _HOLDS.get(fid)
        return dict(h) if h else None


def set_hold(fid: str, evidence: str, repo: str) -> bool:
    """Record a hold. True when it is NEW (the caller logs/comments once, not per poll)."""
    with _LOCK:
        prior = _HOLDS.get(fid)
        _HOLDS[fid] = {"evidence": evidence, "since": (prior or {}).get("since") or time.time(), "repo": repo}
        return prior is None


def clear_hold(fid: str) -> dict | None:
    with _LOCK:
        return _HOLDS.pop(fid, None)


async def check(slug: str, repo: str, patterns: dict, *, cwd: str = ".", now: float | None = None) -> str:
    """``""`` when ``slug`` shows no release in flight, else the evidence sentence
    (``branch prepare-release/v0.173.0``, ``PR #3565 (prepare-release/v0.173.0)``,
    ``prepare-release.yml run in_progress``, or ``freeze check failed: …``)."""
    from . import worktree

    t = time.time() if now is None else now
    key = (slug, repo, tuple(sorted((k, tuple(v)) for k, v in patterns.items())))
    with _LOCK:
        hit = _CHECKS.get(key)
        if hit and t - hit["at"] < CHECK_TTL_S:
            return hit["evidence"]
    evidence: list[str] = []
    try:
        for name in await worktree.remote_branches(repo, patterns.get("branches")):
            evidence.append(f"branch {name}")
        if slug:
            for number, head in await worktree.open_pr_heads(slug, patterns.get("pr_heads"), cwd=cwd):
                evidence.append(f"PR #{number} ({head})")
            for wf in patterns.get("workflows") or ():
                runs = await worktree.active_workflow_runs(slug, wf, cwd=cwd)
                if runs:
                    evidence.append(f"{wf} run {runs[0].get('status')}")
        elif patterns.get("pr_heads") or patterns.get("workflows"):
            raise worktree.WorktreeError("the PR's GitHub repo could not be resolved")
    except Exception as exc:  # noqa: BLE001 — fail CLOSED: an unanswerable check holds the merge
        evidence = [f"freeze check failed: {exc}"]
    sentence = "; ".join(dict.fromkeys(evidence))  # de-dup (a branch AND its PR both match)
    with _LOCK:
        _CHECKS[key] = {"at": t, "evidence": sentence}
    return sentence
