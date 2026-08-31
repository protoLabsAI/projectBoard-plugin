"""Disposable-sandbox PR-lifecycle tier for the three EXEMPT ``worktree.py`` write seams
(#361, slice 3): ``open_pr``, ``_promote_adopted_draft``, ``close_pr``.

These three MUTATE real PR lifecycle state — they create a PR, promote an adopted draft to
ready, and close a PR. Unlike the read-dominant `gh` seams (tests/test_worktree_gh.py), they
CANNOT be exercised against the shared, permanently-open read fixture: each would create a
brand-new PR or consume that fixture. Covering them for real therefore needs a THROWAWAY
sandbox repository — one it is safe to open, promote and close real PRs against — which is
exactly why they are classified EXEMPT in tests/test_external_seams.py rather than REAL.

This file is that exemption's EXECUTABLE escape hatch, and the direct answer to the review
that flagged the UNCOVERED → EXEMPT move: "reclassifying without adding a disposable-sandbox
lifecycle test removes them from the ratchet, so regressions no longer fail the contract."
The lifecycle test now EXISTS and drives all three seams against the sandbox for real — it is
merely DORMANT until the operator provisions the sandbox, the decision the exemption records:

* Unset ``PB_SANDBOX_REPO`` (CI's state, and the default) → every seam test SKIPS. The seams
  stay honestly EXEMPT: no CI job creates / closes / promotes a PR anywhere (acceptance r4).
* Set ``PB_SANDBOX_REPO=owner/name`` to a THROWAWAY repo (never production) with a
  write-capable ``gh`` credential → the seams run for real against it. The day that happens
  these three become REAL and ``MAX_EXEMPT_WORKTREE`` in tests/test_external_seams.py falls.
* Set ``PB_REQUIRE_SANDBOX=1`` to ENFORCE the tier (mirrors PB_REQUIRE_GH / PB_REQUIRE_BR):
  an absent sandbox or credential then FAILS the guard below instead of skipping, so a
  provisioned sandbox that quietly stops working is caught rather than silently green.

Safety: the fixture REFUSES to run if ``PB_SANDBOX_REPO`` resolves to the checkout's own repo
— this tier opens, promotes and closes REAL PRs and must only ever touch a disposable sandbox,
never the production repository (acceptance r4). Nothing escapes ``tmp_path`` (a fresh clone per
run), and the fixture closes every PR it opens and deletes every branch it pushes on teardown.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from conftest import ROOT, gh_credentialed
from project_board import worktree

SANDBOX_REPO = (os.environ.get("PB_SANDBOX_REPO") or "").strip()
_SLUG_RE = re.compile(r"^[^/\s]+/[^/\s]+$")


def sandbox_tier_ready() -> tuple[bool, str]:
    """(ready, reason) for the disposable-sandbox PR-lifecycle tier: a usable write-capable `gh`
    credential AND a configured THROWAWAY sandbox repo. ``reason`` is the local skip message / the
    CI failure message under PB_REQUIRE_SANDBOX. Mirrors ``conftest.gh_tier_ready`` in shape."""
    if not gh_credentialed():
        return False, "no usable `gh` credential (set GH_TOKEN or run `gh auth login`) for the sandbox tier"
    if not SANDBOX_REPO:
        return False, (
            "PB_SANDBOX_REPO is not set — the three PR-lifecycle write seams are EXEMPT until a "
            "disposable sandbox repository is provisioned (the operator's decision, #361 S3). Set "
            "PB_SANDBOX_REPO=owner/name to a THROWAWAY repo (never production) to run this tier for real."
        )
    if not _SLUG_RE.match(SANDBOX_REPO):
        return False, f"PB_SANDBOX_REPO is not an owner/name slug: {SANDBOX_REPO!r}"
    return True, ""


_READY, _REASON = sandbox_tier_ready()
requires_sandbox = pytest.mark.skipif(
    not _READY,
    reason=_REASON or "disposable-sandbox PR-lifecycle tier prerequisites present (set PB_REQUIRE_SANDBOX to enforce)",
)


def _sh(*cmd: str, cwd: str, timeout: float = 120) -> str:
    """A setup/teardown shell-out for the fixture — NEVER the seam under test (those are the
    ``worktree.*`` calls in the tests). Raises on a non-zero exit, returns stripped stdout, exactly
    as tests/test_worktree_git.py's ``_git_run`` and conftest's ``_gh_setup`` do for fixture wiring."""
    proc = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def test_sandbox_tier_cannot_silently_skip_when_required():
    """The skip guard (mirrors ``test_gh_tier_cannot_silently_skip_in_ci`` / #136): deliberately NOT
    under ``@requires_sandbox``. When an operator opts INTO enforcing the exemption's escape hatch
    (``PB_REQUIRE_SANDBOX=1`` — the sandbox is provisioned), an absent sandbox or credential FAILS
    here instead of silently skipping, so a provisioned sandbox that stops working is caught. CI does
    NOT set ``PB_REQUIRE_SANDBOX`` (the operator has not provisioned the sandbox), so this is an
    always-green no-op there and the three seams stay honestly EXEMPT."""
    if os.environ.get("PB_REQUIRE_SANDBOX"):
        ready, reason = sandbox_tier_ready()
        assert ready, (
            f"PB_REQUIRE_SANDBOX is set but the disposable-sandbox PR-lifecycle tier cannot run: {reason}. "
            f"Provision PB_SANDBOX_REPO (a THROWAWAY repo) + a write-capable GH_TOKEN, or unset "
            f"PB_REQUIRE_SANDBOX. A silent skip here is exactly the failure mode #354 shipped through."
        )


class _Sandbox:
    """A fresh clone of the disposable sandbox repo, with per-test branch names and a teardown that
    closes every PR it opened and deletes every branch it pushed — so a run leaks nothing into the
    sandbox and never touches the production repo."""

    def __init__(self, clone: str):
        self.slug = SANDBOX_REPO
        self.clone = clone
        _sh("git", "config", "user.email", "board-sandbox@localhost", cwd=clone)
        _sh("git", "config", "user.name", "Board Sandbox", cwd=clone)
        _sh("git", "config", "commit.gpgsign", "false", cwd=clone)
        self.base = _sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=clone)
        self._counter = 0
        self._branches: list[str] = []

    def branch(self, suffix: str) -> str:
        """A unique branch name for this run — namespaced + counter, so parallel runs and repeated
        tests never collide on the sandbox. Registered for teardown."""
        self._counter += 1
        run_id = (os.environ.get("GITHUB_RUN_ID") or os.environ.get("USER") or "local").replace("/", "-")
        name = f"pb-sandbox/{run_id}-{self._counter}-{suffix}"
        self._branches.append(name)
        return name

    def cleanup(self) -> None:
        """Best-effort: close any PR opened for each branch (deleting its remote branch), then delete
        the branch directly as a backstop when no PR was created. Never raises out of teardown."""
        for br in self._branches:
            subprocess.run(
                ["gh", "pr", "close", br, "--repo", self.slug, "--delete-branch"],
                cwd=self.clone,
                capture_output=True,
                text=True,
            )
            subprocess.run(["git", "push", "origin", "--delete", br], cwd=self.clone, capture_output=True, text=True)


@pytest.fixture
def sandbox(tmp_path):
    """Clone the disposable sandbox repo into ``tmp_path`` via real `gh`, or skip / fail per the
    tier's local / PB_REQUIRE_SANDBOX posture. REFUSES to run against the checkout's own repo — this
    tier mutates real PR state and must only ever touch a throwaway sandbox (acceptance r4)."""
    ready, reason = sandbox_tier_ready()
    if not ready:
        if os.environ.get("PB_REQUIRE_SANDBOX"):
            pytest.fail(
                f"PB_REQUIRE_SANDBOX is set but the disposable-sandbox tier cannot run: {reason}. Fix the "
                f"provisioning (PB_SANDBOX_REPO — a throwaway repo — + a write-capable GH_TOKEN)."
            )
        pytest.skip(reason)
    # NEVER mutate production: the sandbox must not be the checkout's own repo. Resolve the checkout's
    # slug with real `gh` and refuse an accidental match (case-insensitive) before any PR is opened.
    checkout_slug = _sh("gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner", cwd=str(ROOT))
    assert SANDBOX_REPO.lower() != checkout_slug.strip().lower(), (
        f"PB_SANDBOX_REPO ({SANDBOX_REPO!r}) resolves to the checkout's OWN repo — this tier creates, "
        f"promotes and closes REAL PRs and must target a THROWAWAY sandbox, never production (r4)."
    )
    clone = str(tmp_path / "sandbox")
    _sh("gh", "repo", "clone", SANDBOX_REPO, clone, cwd=str(tmp_path))
    sb = _Sandbox(clone)
    try:
        yield sb
    finally:
        sb.cleanup()


@requires_sandbox
async def test_open_pr_creates_a_real_pr_and_close_pr_closes_it(sandbox):
    """``open_pr`` + ``close_pr`` end-to-end against the disposable sandbox — the exact create/close
    lifecycle a mock cannot validate (the shape that shipped #353/#354/#356). ``open_pr`` commits the
    worktree, pushes the branch and shells ``gh pr create`` FOR REAL, returning a live PR URL that
    reads back OPEN; ``close_pr`` then shells ``gh pr close`` FOR REAL and the PR reads back CLOSED."""
    branch = sandbox.branch("create")
    _sh("git", "checkout", "-b", branch, sandbox.base, cwd=sandbox.clone)
    # Leave the change UNCOMMITTED: open_pr's commit_worktree stages + commits it, so this exercises
    # the whole "coder left work in the tree → PR" path, not just a pre-made commit.
    with open(os.path.join(sandbox.clone, f"{branch.replace('/', '_')}.txt"), "w") as fh:
        fh.write("disposable sandbox lifecycle probe — safe to delete\n")

    url = await worktree.open_pr(
        sandbox.clone,
        branch,
        base=sandbox.base,
        title="[sandbox] open_pr lifecycle probe",
        body="disposable — opened by the #361 S3 sandbox tier; safe to close/delete",
        promote_draft=False,
    )
    assert url and "/pull/" in url, f"open_pr did not return a PR URL: {url!r}"
    assert await worktree.pr_state(url, cwd=sandbox.clone) == "OPEN"

    ok, detail = await worktree.close_pr(url, comment="sandbox lifecycle cleanup", cwd=sandbox.clone)
    assert ok is True, f"close_pr failed: {detail!r}"
    assert await worktree.pr_state(url, cwd=sandbox.clone) == "CLOSED"


@requires_sandbox
async def test_promote_adopted_draft_marks_a_real_draft_ready(sandbox):
    """``_promote_adopted_draft`` against a real DRAFT PR (#207: the coder opened its own
    ``gh pr create --draft`` before the loop arrived). Setup opens a real draft; the seam then shells
    ``gh pr ready`` FOR REAL and the PR flips ``isDraft`` False. A mock proves neither that GitHub
    accepts the ready call nor that the credential may make it — the gap the exemption records."""
    branch = sandbox.branch("draft")
    _sh("git", "checkout", "-b", branch, sandbox.base, cwd=sandbox.clone)
    fname = f"{branch.replace('/', '_')}.txt"
    with open(os.path.join(sandbox.clone, fname), "w") as fh:
        fh.write("disposable sandbox draft probe — safe to delete\n")
    _sh("git", "add", "-A", cwd=sandbox.clone)
    _sh("git", "commit", "-m", "sandbox draft probe", cwd=sandbox.clone)
    _sh("git", "push", "-u", "origin", branch, cwd=sandbox.clone)
    # Setup: open the PR AS A DRAFT the way a coder would — this is the state _promote_adopted_draft
    # exists to resolve, so it is fixture wiring, not the seam under test.
    url = _sh(
        "gh",
        "pr",
        "create",
        "--head",
        branch,
        "--base",
        sandbox.base,
        "--title",
        "[sandbox] draft probe",
        "--body",
        "disposable — opened as a draft by the #361 S3 sandbox tier; safe to close/delete",
        "--draft",
        cwd=sandbox.clone,
    )
    before = await worktree.pr_merge_info(url, cwd=sandbox.clone)
    assert before["isDraft"] is True, f"setup did not open a draft: {before!r}"

    await worktree._promote_adopted_draft(url, branch, cwd=sandbox.clone)

    after = await worktree.pr_merge_info(url, cwd=sandbox.clone)
    assert after["isDraft"] is False, f"_promote_adopted_draft did not mark the draft ready: {after!r}"
