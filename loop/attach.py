"""Attach an externally opened PR to the card it belongs to (#402): the operator/PM verb
beside the loop's own adoption edges.

The loop can already adopt a PR it did not see opened. Crash recovery
(``_reconcile_orphan``) finds the PR whose head is the card's canonical branch and moves the
card to in_review. A PR a HUMAN opened had no edge at all: recovered work promoted onto a
proper branch, or a fix made by hand. On 2026-09-07 bd-ezs7's recovered implementation went
up as PR #3369 on the card's own branch. The card sat terminal-blocked while the PR went
through CI and review invisible to the board. Re-dispatching the card would have
force-removed the candidate worktree the work still lived in. The PM hand-closed the card
after the merge.

This is that edge, deliberately no wider than recovery's adoption:

- **The PR must be the one the board would find for the card itself.** It must be OPEN, in
  the card's project repo and not a fork, with its head on the card's canonical branch and
  its base on the project's base. Every later edge keys on that branch name: a CI or review
  fix round resumes ``origin/<canonical branch>``, and so do recovery and the reap. A PR on
  any other branch would be abandoned by the first fix round, which would then open a
  second PR beside it.
- **The card must be an in-flight coding card with no PR of its own under review and no
  open dependency** (``store.attach_refusal``). A card whose earlier PR was CLOSED (rejected,
  then reworked on the same branch into a new one) may take the new PR in its place. An
  earlier PR that is still open or already merged is refused. The loop must not be working
  the card: no live drive, no claimed build, no review gate in flight. A live drive would
  race the attach into a second PR. That is refused, not overridden: wait for it to end.
- **The write runs under the loop's claim lock,** the one the tick and ``board_dispatch``
  hold around the claim scan. A ready card is exactly what that scan takes, so it cannot be
  claimed and dispatched halfway through becoming in_review.

Afterwards the card is in_review with the PR on ``external_ref``. That is the state the
loop's own ``open_review`` leaves, so the ordinary reconcile drives it from there: CI,
rebase, merge → done. When the review gate is on, the attach arms it with
``review-pending``, so the gate reviews the attached head. Otherwise the merge edge would
wait forever for a verdict. The attach itself is an audit comment on the card.
"""

from __future__ import annotations

import contextlib
import sys

from ._common import *  # noqa: F401,F403 — share the loop kernel namespace

_loop = sys.modules[__package__]  # the loop package, for monkeypatch-visible seams


def _worked_by_the_loop(fid: str) -> str:
    """Why the loop is still working ``fid`` right now, or ``""``. These are the three
    signals that span a card's time in a live drive, the same liveness guard the review
    reconcile applies before it touches a card (#340, #323)."""
    if _loop.live_drive(fid) is not None:
        return (
            f"{fid} has a live coder drive — attaching now would race it into a second PR; wait for the "
            "drive to finish (or fail), then attach"
        )
    loop = _loop.live_loop()
    if loop is not None and (fid in loop._inflight_files or fid in loop._review_inflight):
        return f"{fid} is still being worked by the loop (a claimed build or a running review gate) — attach once it settles"
    return ""


async def _github_refusal(feature: dict, facts: dict, pr_url: str, *, repo: str, base: str) -> str:
    """The GitHub half of the attach checks: is ``facts`` (``worktree.pr_identity``) the PR
    the board would find for ``feature`` itself? Returns ``""`` when it is."""
    fid = feature.get("id", "")
    if not facts:
        return (
            f"could not read {pr_url} with gh from {repo} — check the url, and that gh is installed and authenticated"
        )
    project_slug = await worktree.repo_slug(cwd=repo)
    if not project_slug:
        return f"could not resolve the GitHub repo of {fid}'s project checkout {repo} — nothing to check the PR against"
    _number, pr_slug = _parse_pr_url(facts["url"])
    if pr_slug.casefold() != project_slug.casefold():
        return f"{facts['url']} is in {pr_slug}, but {fid} builds in {project_slug} ({repo})"
    if facts.get("cross_repo") is not False:
        return (
            f"{facts['url']} comes from a fork (or gh could not say) — fix rounds push to this repo's branch, "
            "so the board can only adopt a PR opened from a branch in this repo"
        )
    state = facts.get("state")
    if state == "MERGED":
        return (
            f"{facts['url']} is already merged — there is nothing left to review; record the shipped work with "
            f"board_mark_done({fid}) (POST /features/{fid}/done)"
        )
    if state == "CLOSED":
        return f"{facts['url']} was closed without merging — reopen it before attaching it"
    if state != "OPEN":
        return f"{facts['url']} is in state {state!r}, not OPEN"
    branch = worktree.branch_name(fid, feature.get("title") or "")
    if facts.get("head") != branch:
        return (
            f"{facts['url']} is on branch {facts.get('head')!r}, not {fid}'s canonical branch — push the work "
            f"to branch {branch!r}, open the PR from it, and attach that PR. Every later board edge (fix rounds, "
            "recovery, the reap) works on that branch, so a PR from any other would be abandoned by the first "
            "fix round"
        )
    if facts.get("base") != base:
        return (
            f"{facts['url']} targets {facts.get('base')!r}, but {fid}'s project merges into {base!r} — retarget "
            "the PR's base, then attach"
        )
    return ""


async def attach_external_pr(
    store,
    feature: dict,
    pr_url: str,
    *,
    repo: str,
    base: str,
    review_gate: bool = False,
    reason: str = "",
    by: str = "",
) -> dict:
    """Attach ``pr_url`` to ``feature`` (a projection the caller just read) and return
    ``{id, state, pr_url, review_pending, already_attached}``. ``repo`` and ``base`` are
    the checkout and base branch the card builds against, resolved as every tool and route
    edge resolves them (``api.repo_for_feature`` / ``base_branch_for_feature``).
    ``review_gate`` is the board's live knob.

    The checks run in order of cost. The board's own shape comes first and is free. Next is
    the loop's liveness, in memory. Last are the ``gh`` reads. The write re-checks
    liveness under the claim lock, and the store re-checks the card's shape on a fresh read,
    so nothing decided on a stale view is ever written. Every refusal raises ``BoardError``
    naming what to do instead.

    A card that already carries a DIFFERENT PR takes the new one only once ``gh`` says the
    old one is CLOSED: a PR rejected and reworked on the same branch. An open prior PR would
    be orphaned. A merged one means the card is done, and the reconcile closes it."""
    fid = str(feature.get("id") or "")
    pr_url = str(pr_url or "").strip()
    current = str(feature.get("pr_url") or "").strip()
    replaces = current if current and store_mod.pr_key(current) != store_mod.pr_key(pr_url) else ""
    # `replaces` passes the one-PR rule PROVISIONALLY here; the gh read below settles it.
    refusal = store_mod.attach_refusal(feature, pr_url, replaces=replaces)
    if refusal:
        raise BoardError(refusal)
    if feature.get("board_state") == "in_review":  # its own PR, already attached
        return {
            "id": fid,
            "state": "in_review",
            "pr_url": feature.get("pr_url", ""),
            "review_pending": LABEL_REVIEW_PENDING in (feature.get("labels") or []),
            "already_attached": True,
        }
    busy = _worked_by_the_loop(fid)
    if busy:
        raise BoardError(busy)
    if replaces:
        prior = await worktree.pr_state(replaces, cwd=repo)
        if prior != "CLOSED":
            raise BoardError(
                f"{fid} already carries PR {replaces}, which is "
                + {
                    "OPEN": "still open — attaching another would orphan it; close it first, or attach that one",
                    "MERGED": "already merged — the card is done; the PR reconcile closes it",
                }.get(prior, "unreadable with gh — the board only replaces a PR it can see is closed")
            )
    facts = await worktree.pr_identity(pr_url, cwd=repo)
    refusal = await _github_refusal(feature, facts, pr_url, repo=repo, base=base)
    if refusal:
        raise BoardError(refusal)
    loop = _loop.live_loop()
    async with loop._claim_guard() if loop is not None else contextlib.nullcontext():
        busy = _worked_by_the_loop(fid)  # re-checked where no claim can interleave
        if busy:
            raise BoardError(busy)
        attached = await asyncio.to_thread(
            store.attach_pr,
            fid,
            facts["url"],
            reason=reason,
            by=by,
            review_pending=review_gate,
            replaces=replaces,
        )
    log.info(
        "[project_board] %s attached external PR %s (was %s) → in_review%s",
        fid,
        facts["url"],
        feature.get("board_state"),
        " + review-pending" if review_gate else "",
    )
    return {
        "id": attached["id"],
        "state": attached["board_state"],
        "pr_url": attached.get("pr_url", ""),
        "review_pending": LABEL_REVIEW_PENDING in (attached.get("labels") or []),
        "already_attached": False,
    }
