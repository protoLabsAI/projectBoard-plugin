"""The external-seam coverage ratchet.

Three fixes shipped GREEN and were inert, in one night, because each one's whole
purpose was to make an EXTERNAL system do something and each was validated only
against a mock of that system:

* **#353** — `review-clean-sha:<40-char sha>` is 57 characters; beads caps a label at 50
  and refuses the whole `br update`. The fake `_run` has no validator, so 1,650 tests
  passed while the pin could never be written and the card blocked.
* **#354** — `POST /check-runs` is GitHub-App-only and 403s under the board's PAT. The
  `gh` mock returned success, so the review verdict "published" to nothing, for a day.
* **#356** — `br update --claim` refuses a bead already assigned to somebody else, so a
  task assigned to a dispatch target livelocked. Again invisible to a fake `br`.

A mock proves the plugin's call SHAPE and its local branching. It cannot prove that a
real binary accepts the payload, that the configured credential holds the permission, or
that the provider behaves as assumed. For a function whose entire job is an external
effect, a mocked test is close to no test — and worse, it reports success.

This file does not create that coverage. It makes its ABSENCE impossible to add silently:

1. every function that shells an external system must be CLASSIFIED here, so a new
   external call site cannot land without someone stating how it is validated;
2. the UNCOVERED count is a RATCHET — it may fall, never rise. The lists below are a
   burndown, exactly like the `ignore_imports` contract in protoAgent core: remove from
   them, never add.

`REAL` means the function is exercised against the real binary/API in the integration
tier. `EXEMPT: <reason>` is for a seam where real coverage genuinely is not warranted —
state why. `UNCOVERED` is honest debt.
"""

from __future__ import annotations

import ast
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# ── the registry ────────────────────────────────────────────────────────────────────
# worktree.py — shells `gh` and `git`. NOTHING here is covered against the real API
# today: there is no GitHub sandbox tier at all, which is precisely how #354 shipped.
WORKTREE_SEAMS: dict[str, str] = {
    "_find_marked_comment": "UNCOVERED",
    "_promote_adopted_draft": "UNCOVERED",
    "base_checkout_dirt": "UNCOVERED",
    "close_pr": "UNCOVERED",
    "commit_worktree": "UNCOVERED",
    "create_worktree": "UNCOVERED",
    "delete_remote_branch": "UNCOVERED",
    "merge_pr": "UNCOVERED",
    "merged_state_worktree": "UNCOVERED",
    "open_pr": "UNCOVERED",
    "origin_head_sha": "UNCOVERED",
    "post_or_update_pr_comment": "UNCOVERED",
    "post_review_status": "UNCOVERED",
    "pr_ci_status": "UNCOVERED",
    "pr_diff": "UNCOVERED",
    "pr_head_sha": "UNCOVERED",
    "pr_merge_info": "UNCOVERED",
    "pr_state": "UNCOVERED",
    "pr_url_for_branch": "UNCOVERED",
    "promote_worktree": "UNCOVERED",
    "prune_stale_worktrees": "UNCOVERED",
    "read_review_status": "UNCOVERED",
    "rebase_onto_base": "UNCOVERED",
    "remove_worktree": "UNCOVERED",
    "repo_slug": "UNCOVERED",
    "stage_all": "UNCOVERED",
}

# store.py — shells `br`. This is the strong tier: CI runs a real pinned binary across
# a version matrix (0.1.23 / 0.2.16 / 0.3.2) with PB_REQUIRE_BR=1 so an absent binary
# FAILS rather than skips. #353 and #356 were both caught by adding to it.
STORE_SEAMS: dict[str, str] = {
    "_create": "UNCOVERED",
    "_find_by_external_ref": "UNCOVERED",
    "_open_blockers": "REAL",
    "_prepare_ready": "UNCOVERED",
    "add_dependency": "REAL",
    "archive_stale": "UNCOVERED",
    "block_from_review": "UNCOVERED",
    "bounce_ci_fail": "UNCOVERED",
    "cancel_feature": "REAL",
    "claim": "REAL",
    "claim_next_ready": "REAL",
    "claim_task": "REAL",
    "clear_blocked": "UNCOVERED",
    "clear_budgets": "REAL",
    "clear_verified_candidate": "UNCOVERED",
    "comment": "UNCOVERED",
    "create_feature": "REAL",
    "create_from_plan": "UNCOVERED",
    "delete_feature": "UNCOVERED",
    "escalate": "REAL",
    "feature_comments": "UNCOVERED",
    "flag_blocked": "REAL",
    "get_feature": "REAL",
    "list_features": "REAL",
    "mark_designing": "UNCOVERED",
    "mark_done": "UNCOVERED",
    "mark_ready": "REAL",
    "open_review": "REAL",
    "raw_features_with_comments": "REAL",
    "ready_queue": "REAL",
    "record_attempt": "UNCOVERED",
    "record_budget": "REAL",
    "record_delivery": "UNCOVERED",
    "record_gens_spent": "REAL",
    "record_merge": "REAL",
    "record_merged_verified": "REAL",
    "record_reviewed_head": "UNCOVERED",
    "record_verification": "UNCOVERED",
    "record_verified_candidate": "REAL",
    "remove_dependency": "UNCOVERED",
    "requeue": "UNCOVERED",
    "set_requirements": "UNCOVERED",
    "set_review_substate": "REAL",
    "update_feature": "REAL",
}

# The ratchet. These are the counts at the moment the contract was introduced; a change
# that raises either number fails this file. Lower them as coverage lands.
MAX_UNCOVERED_WORKTREE = 26
MAX_UNCOVERED_STORE = 21


def _external_seams(path: str, callees: set[str], skip_prefix: tuple[str, ...] = ()) -> set[str]:
    """Every function in ``path`` that reaches an external system, by AST — not by name
    convention, so a seam cannot hide behind a rename."""
    tree = ast.parse((_ROOT / path).read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in callees or node.name.startswith(skip_prefix):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
                if name in callees:
                    found.add(node.name)
                    break
    return found


def _classify(registry: dict[str, str], found: set[str], module: str) -> None:
    missing = sorted(found - set(registry))
    assert not missing, (
        f"{module}: {len(missing)} external seam(s) are not classified in the coverage "
        f"registry: {missing}. A function that shells an external system must declare how "
        f"it is validated — REAL (exercised against the real binary/API in the integration "
        f"tier), or 'EXEMPT: <why not>'. Marking it UNCOVERED is allowed and honest, but it "
        f"counts against the ratchet below, which may not rise."
    )
    stale = sorted(set(registry) - found)
    assert not stale, (
        f"{module}: the registry names {len(stale)} function(s) that no longer reach an "
        f"external system: {stale}. Drop them — a stale entry inflates the ratchet's "
        f"headroom and lets real debt in unnoticed."
    )


def test_every_worktree_external_seam_is_classified():
    _classify(WORKTREE_SEAMS, _external_seams("worktree.py", {"_gh", "_git"}, ("_git",)), "worktree.py")


def test_every_store_external_seam_is_classified():
    _classify(STORE_SEAMS, _external_seams("store.py", {"_run"}), "store.py")


def test_uncovered_seams_never_increase():
    """The ratchet. Uncovered external seams are debt we can pay down but must not add to.

    If this fails on a change that ADDS a seam: give it real coverage, or justify an
    EXEMPT. If it fails because you covered one: lower the constant, that is the point."""
    w = sum(1 for v in WORKTREE_SEAMS.values() if v == "UNCOVERED")
    s = sum(1 for v in STORE_SEAMS.values() if v == "UNCOVERED")
    assert w <= MAX_UNCOVERED_WORKTREE, (
        f"worktree.py uncovered external seams rose to {w} (ratchet: {MAX_UNCOVERED_WORKTREE}). "
        "Every one of these is a function whose only job is an external effect, validated "
        "solely against a mock of that effect — the shape that shipped #353, #354 and #356."
    )
    assert s <= MAX_UNCOVERED_STORE, f"store.py uncovered external seams rose to {s} (ratchet: {MAX_UNCOVERED_STORE})"


def test_registry_values_are_wellformed():
    for name, value in {**WORKTREE_SEAMS, **STORE_SEAMS}.items():
        assert value == "REAL" or value == "UNCOVERED" or value.startswith("EXEMPT: "), (
            f"{name}: {value!r} is not a valid classification. Use REAL, UNCOVERED, or "
            f"'EXEMPT: <reason>' — an exemption without a stated reason is not one."
        )


def test_the_real_tier_actually_runs_against_a_real_binary():
    """The registry's REAL entries are only worth anything if the integration tier is not
    silently skipped. CI sets PB_REQUIRE_BR=1 so an absent `br` FAILS the run (#136)."""
    integ = (_ROOT / "tests" / "test_integration.py").read_text()
    assert "PB_REQUIRE_BR" in integ
    assert re.search(r"requires_br\s*=\s*pytest\.mark\.skipif", integ)
