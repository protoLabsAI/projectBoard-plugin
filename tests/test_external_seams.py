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

EXEMPT is not grantable by say-so: because it drops a seam from the ratchet, an exemption
is VERIFIED against the source. The only worktree seams that may be EXEMPT are the three
PR-lifecycle writes, and each must provably shell a fixture-destroying `gh pr` write
(`create` / `close` / `ready`) — a mutation that creates a new PR or consumes the pinned
permanently-open read fixture, so it needs a disposable sandbox repo to cover for real.
Symmetrically, no REAL seam may shell one of those writes. So a read cannot hide under
EXEMPT to dodge the ratchet, and a mock cannot pose as REAL while mutating real PR state.

And EXEMPT is not an escape FROM the ratchet either — it is a second, equally strict ratchet,
answering the exact review that flagged the UNCOVERED → EXEMPT move: dropping a seam from the
UNCOVERED count must not make its debt vanish. So `MAX_EXEMPT_WORKTREE` may only FALL (each
write flips to REAL the day a disposable sandbox repo is provisioned), never rise — a fourth
EXEMPT cannot be minted to dodge the uncovered-seam ratchet. And the exemption is backed by an
EXECUTABLE escape hatch: tests/test_worktree_pr_lifecycle_sandbox.py drives all three writes —
create, promote-draft, close — against a THROWAWAY sandbox repo through the real `gh`, dormant
(skipped) only until the operator sets PB_SANDBOX_REPO (with PB_REQUIRE_SANDBOX to enforce it).
So the gap the exemption records is one env var from closing, not a promise on paper, and a
regression that stops a seam issuing its `gh pr` write fails this contract now, not silently.
"""

from __future__ import annotations

import ast
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# ── the registry ────────────────────────────────────────────────────────────────────
# worktree.py — shells `gh` and `git`. The 11 LOCAL-git seams (no network, no credential)
# are exercised against a real git repo in tests/test_worktree_git.py — a temporary bare origin
# plus a clone, so `origin/<base>` and PR-branch resume resolve exactly as in production (#361,
# slice 1). The 12 read-dominant `gh`/GitHub seams are now exercised against a PINNED,
# permanently-open PR in tests/test_worktree_gh.py (#361, slice 2): CI sets PB_REQUIRE_GH=1 so an
# absent/unusable credential FAILS instead of skipping — the same posture that made the real-`br`
# tier catch #353/#356, applied to the read seams a mocked `_gh` was blind to (how #354 shipped).
#
# The 3 WRITE-lifecycle seams (open_pr / close_pr / _promote_adopted_draft) are classified EXEMPT
# (#361, slice 3), NOT UNCOVERED. Each mutates real PR lifecycle state — create a PR, close a PR,
# promote an adopted draft to ready — so a REAL tier could only run by creating and tearing down
# real PRs against a DISPOSABLE SANDBOX repository, and the operator's decision is not to provision
# that sandbox now. This is an honest gap, deliberately recorded, not mock confidence: these paths
# run REPEATEDLY in normal board operation (every delivery opens a PR, closes stale ones, promotes
# adopted drafts), so they ARE exercised in production. The residual weakness is therefore the
# FEEDBACK CHANNEL — a regression here surfaces as a blocked card / operator signal rather than red
# CI — not an absence of runtime execution. They are NOT mocked into a false REAL, and NO CI job
# creates/closes/promotes a PR in a production repository.
#
# EXEMPT does not remove these three from accountability — that was the review finding on the
# UNCOVERED → EXEMPT move. Two things keep them honest: (1) MAX_EXEMPT_WORKTREE is a SECOND ratchet
# that may only fall (each flips to REAL when a disposable sandbox is provisioned), so debt cannot be
# dissolved by relabelling; and (2) their exemption is backed by an EXECUTABLE escape hatch —
# tests/test_worktree_pr_lifecycle_sandbox.py drives all three against a THROWAWAY sandbox repo for
# real, dormant only until the operator sets PB_SANDBOX_REPO. The escape hatch's presence + coverage
# of each seam is itself asserted below, so the exemption is one env var from closing, not paper.
WORKTREE_SEAMS: dict[str, str] = {
    "_find_marked_comment": "REAL",
    "_promote_adopted_draft": (
        "EXEMPT: promotes a real adopted draft PR to ready — REAL coverage would need a disposable "
        "sandbox repo to hold that PR lifecycle state (operator chose not to provision one now). "
        "Run on every adopted-draft delivery in production; residual risk is delayed feedback via a "
        "blocked card / operator signal, not absent execution."
    ),
    "base_checkout_dirt": "REAL",
    "close_pr": (
        "EXEMPT: closes a real PR — REAL coverage would need a disposable sandbox repo to open and "
        "tear down that PR (operator chose not to provision one now). Run routinely in production; "
        "residual risk is delayed feedback via a blocked card / operator signal, not absent execution."
    ),
    "commit_worktree": "REAL",
    "create_worktree": "REAL",
    "delete_remote_branch": "REAL",
    "merge_pr": "REAL",
    "merged_state_worktree": "REAL",
    "open_pr": (
        "EXEMPT: creates a real PR — REAL coverage would need a disposable sandbox repo to hold the "
        "created PR (operator chose not to provision one now). Run on every delivery in production; "
        "residual risk is delayed feedback via a blocked card / operator signal, not absent execution."
    ),
    "origin_head_sha": "REAL",
    "post_or_update_pr_comment": "REAL",
    "post_review_status": "REAL",
    "pr_ci_status": "REAL",
    "pr_diff": "REAL",
    "pr_head_sha": "REAL",
    "pr_merge_info": "REAL",
    "pr_state": "REAL",
    "pr_url_for_branch": "REAL",
    "promote_worktree": "REAL",
    "prune_stale_worktrees": "REAL",
    "read_review_status": "REAL",
    "rebase_onto_base": "REAL",
    "remove_worktree": "REAL",
    "repo_slug": "REAL",
    "stage_all": "REAL",
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
# that raises either number fails this file. Lower them as coverage lands. worktree.py fell
# 15 → 3 when the 12 read-dominant `gh` seams landed real coverage (#361, slice 2), then 3 → 0
# when the last 3 write-lifecycle seams were classified EXEMPT (#361, slice 3): every worktree
# seam is now REAL or an honestly-recorded EXEMPT, so the worktree UNCOVERED floor is 0 and a
# newly-added UNCOVERED worktree seam fails this file outright.
MAX_UNCOVERED_WORKTREE = 0
MAX_UNCOVERED_STORE = 21

# The EXEMPT ratchet. An EXEMPT drops a seam from MAX_UNCOVERED_WORKTREE, so EXEMPT must itself be
# bounded or the label would let real debt vanish (the review finding on the UNCOVERED → EXEMPT
# move). Exactly the three PR-lifecycle writes are EXEMPT today; this floor may only FALL as each
# flips to REAL when a disposable sandbox repo is provisioned — never rise. A fourth EXEMPT fails
# this file just as loudly as a new UNCOVERED one would.
MAX_EXEMPT_WORKTREE = 3


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


# The fixture-destroying `gh pr` write subcommands — the ones that either CREATE a brand-new PR
# or CONSUME / flip the pinned, permanently-open PR the real-`gh` read tier (#361, slice 2) runs
# against. They cannot be exercised against that shared read fixture the way `merge_pr` can: branch
# protection REFUSES `gh pr merge` on the pinned PR, so merge_pr runs for real and leaves the fixture
# intact (that is exactly why merge_pr stays REAL and `merge` is deliberately NOT in this set). These
# three are the only writes that would need a DISPOSABLE SANDBOX repo to cover for real — which is
# why they, and only they, are EXEMPT.
_FIXTURE_DESTROYING_PR_WRITES = {"create", "close", "ready"}


def _gh_pr_subcommands(func_name: str) -> set[str]:
    """Every ``gh pr <sub>`` subcommand ``worktree.<func_name>`` shells with LITERAL leading args,
    by AST. This ties an EXEMPT classification to what the code ACTUALLY does rather than to an
    author's comment: a seam cannot be dropped from the ratchet as EXEMPT unless its source really
    issues a PR-lifecycle write, and a seam kept REAL cannot quietly start issuing one."""
    tree = ast.parse((_ROOT / "worktree.py").read_text())
    subs: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != func_name:
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if (getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)) != "_gh":
                continue
            literals = [a.value for a in sub.args if isinstance(a, ast.Constant)]
            if len(literals) >= 2 and literals[0] == "pr":
                subs.add(literals[1])
    return subs


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


def test_exempt_worktree_seams_are_a_ratchet_that_only_falls():
    """The direct answer to the review finding: reclassifying a seam UNCOVERED → EXEMPT drops it
    from ``MAX_UNCOVERED_WORKTREE``, so without a second bound the label would be a blank cheque
    that makes real debt vanish and lets a regression in opening/closing/promoting a PR pass green.

    EXEMPT is therefore itself ratcheted. The count may FALL — each PR-lifecycle write flips to REAL
    the day a disposable sandbox repo is provisioned — but NEVER rise: a fourth EXEMPT cannot be
    minted to escape the uncovered-seam ratchet, and fails this file exactly as a new UNCOVERED seam
    would."""
    e = sum(1 for v in WORKTREE_SEAMS.values() if v.startswith("EXEMPT: "))
    assert e <= MAX_EXEMPT_WORKTREE, (
        f"worktree.py EXEMPT external seams rose to {e} (ratchet: {MAX_EXEMPT_WORKTREE}). EXEMPT is "
        f"tracked debt, not a way out of the ratchet: it may only fall (a write flips to REAL when a "
        f"disposable sandbox repo is provisioned), never rise. Give the new seam REAL coverage."
    )


def test_worktree_coverage_contract_is_23_real_3_exempt_0_uncovered():
    """The final worktree coverage contract after #361 S1/S2/S3: 23 REAL, 3 EXEMPT, 0 UNCOVERED.

    Every worktree seam is exercised against the real binary/API (REAL) EXCEPT the three PR-lifecycle
    WRITES — open_pr / close_pr / _promote_adopted_draft — which are honestly EXEMPT: each creates,
    closes or promotes a real PR, so making it REAL would require a disposable sandbox repository the
    operator has chosen not to provision now. That is a recorded gap, not mock confidence — these
    paths run on every delivery in production, so the residual risk is delayed feedback (a blocked
    card / operator signal) rather than red CI, NOT an absence of execution. No mocked test is
    relabeled REAL and no CI job creates/closes/promotes a PR in a production repo."""
    real = sorted(k for k, v in WORKTREE_SEAMS.items() if v == "REAL")
    exempt = sorted(k for k, v in WORKTREE_SEAMS.items() if v.startswith("EXEMPT: "))
    uncovered = sorted(k for k, v in WORKTREE_SEAMS.items() if v == "UNCOVERED")

    assert exempt == ["_promote_adopted_draft", "close_pr", "open_pr"], (
        "the ONLY worktree seams that may be EXEMPT are the three PR-lifecycle writes "
        "(open_pr / close_pr / _promote_adopted_draft); every other worktree seam must be REAL. "
        f"Got EXEMPT={exempt}"
    )
    assert len(real) == 23, f"expected 23 REAL worktree seams, got {len(real)}: {real}"
    assert len(exempt) == 3, f"expected 3 EXEMPT worktree seams, got {len(exempt)}: {exempt}"
    assert uncovered == [], (
        f"no worktree seam may remain UNCOVERED after #361 S3 (MAX_UNCOVERED_WORKTREE=0): {uncovered}"
    )
    assert MAX_UNCOVERED_WORKTREE == 0
    assert MAX_EXEMPT_WORKTREE == 3

    # Each exemption must record WHY it cannot be REAL (a disposable sandbox repo for real PR state)
    # AND that the gap is feedback latency, not missing production execution — so the exemption stays
    # honest and can never be read as "this path is untested/unused".
    for name in exempt:
        reason = WORKTREE_SEAMS[name]
        assert "sandbox" in reason.lower(), (
            f"{name}: an EXEMPT PR-lifecycle write must state it needs a disposable sandbox "
            f"repository (it creates/changes real PR state); got {reason!r}"
        )
        assert "production" in reason.lower(), (
            f"{name}: the exemption must record that this path runs in production (the gap is "
            f"delayed feedback, not absent execution); got {reason!r}"
        )


def test_exempt_worktree_seams_really_perform_a_pr_lifecycle_write():
    """The exemption is verified against the SOURCE, not taken on the author's word.

    An EXEMPT classification drops a seam from the uncovered-seam ratchet, so it must not be
    grantable by assertion alone — that is precisely the reviewer's worry (a regression in a
    merely-asserted-exempt seam leaves the contract green). Here every EXEMPT worktree seam must
    provably shell a fixture-destroying `gh pr` write (create / close / ready): a mutation that
    creates a new PR or consumes the pinned permanently-open PR the real-`gh` read tier runs
    against, so it genuinely needs a disposable sandbox repo to cover for real. A plain read seam
    therefore CANNOT be mislabeled EXEMPT to dodge the ratchet — its source issues no such write
    and this test fails."""
    exempt = sorted(k for k, v in WORKTREE_SEAMS.items() if v.startswith("EXEMPT: "))
    assert exempt, "expected at least one EXEMPT worktree seam to verify"
    for name in exempt:
        writes = _gh_pr_subcommands(name) & _FIXTURE_DESTROYING_PR_WRITES
        assert writes, (
            f"{name} is classified EXEMPT but its source shells no fixture-destroying `gh pr` write "
            f"(create/close/ready). An EXEMPT that removes a seam from the ratchet must be a real "
            f"PR-lifecycle mutation needing a disposable sandbox repo — not a read hiding behind the "
            f"label. Give it REAL coverage or reclassify it honestly."
        )


def test_exempt_pr_writes_are_backed_by_an_executable_sandbox_lifecycle_test():
    """The review's remedy, made concrete and enforced. An EXEMPT PR-lifecycle write is honest only
    if the disposable-sandbox lifecycle test that WOULD cover it for real actually EXISTS and drives
    it — otherwise the exemption is a paper promise, and a regression in opening / closing / promoting
    a PR leaves the coverage contract green (the reviewer's exact worry).

    ``tests/test_worktree_pr_lifecycle_sandbox.py`` is that escape hatch: it shells the real `gh`
    against a THROWAWAY sandbox repo to create, promote and close real PRs through the three seams,
    dormant (skipped) only until the operator sets ``PB_SANDBOX_REPO`` (and enforces it with
    ``PB_REQUIRE_SANDBOX``) — the provisioning decision the exemption records. This test asserts the
    escape hatch is present, gates on those env vars, and exercises EACH EXEMPT seam BY NAME, so
    deleting or hollowing it fails the contract rather than silently re-opening the gap. Iterating
    over the live EXEMPT set (not a hard-coded list) keeps the two in lockstep automatically."""
    exempt = sorted(k for k, v in WORKTREE_SEAMS.items() if v.startswith("EXEMPT: "))
    assert exempt, "expected at least one EXEMPT worktree seam whose escape hatch to verify"
    src = (_ROOT / "tests" / "test_worktree_pr_lifecycle_sandbox.py").read_text()
    assert "PB_SANDBOX_REPO" in src and "PB_REQUIRE_SANDBOX" in src, (
        "the disposable-sandbox lifecycle tier must gate on PB_SANDBOX_REPO (provision) and "
        "PB_REQUIRE_SANDBOX (enforce) so it stays dormant until the operator provisions the sandbox — "
        "an EXEMPT must never quietly become a tier that mutates a live repo on every run"
    )
    for name in exempt:
        assert f"worktree.{name}(" in src, (
            f"{name} is EXEMPT but the disposable-sandbox lifecycle test does not exercise "
            f"worktree.{name}(). An exemption must be backed by the executable escape hatch that "
            f"covers it for real, not by its classification comment alone — add the seam to "
            f"tests/test_worktree_pr_lifecycle_sandbox.py or reclassify it honestly."
        )


def test_no_real_worktree_seam_creates_closes_or_promotes_a_pr():
    """The other half of the invariant, and the direct answer to the ratchet-goes-green risk: no
    seam classified REAL may shell a fixture-destroying `gh pr` write. If a REAL seam ever starts
    to create / close / ready a PR, one of two things is true — it now needs the sandbox (→ EXEMPT),
    or a mock is being passed off as REAL against real PR-lifecycle state (acceptance r4) — and both
    must fail LOUDLY here rather than leave the coverage contract green. `merge_pr` stays REAL
    precisely because branch protection REFUSES `gh pr merge` on the pinned fixture, so it runs for
    real without consuming it; `merge` is intentionally excluded from the fixture-destroying set."""
    for name, value in WORKTREE_SEAMS.items():
        if value != "REAL":
            continue
        writes = _gh_pr_subcommands(name) & _FIXTURE_DESTROYING_PR_WRITES
        assert not writes, (
            f"{name} is classified REAL but its source shells a fixture-destroying `gh pr` write "
            f"{sorted(writes)}, which cannot be covered against the shared real-`gh` read fixture. "
            f"Either it now needs a disposable sandbox repo (reclassify EXEMPT) or a mock is posing "
            f"as REAL — neither may leave this contract green."
        )


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


def test_the_real_gh_tier_actually_runs_against_real_github():
    """The `gh`/GitHub REAL entries (#361, slice 2) carry the same guard as the `br` tier: the
    real-GitHub seam tests must be gated by a `requires_gh` skipif AND CI must set PB_REQUIRE_GH=1
    so an absent/unusable credential FAILS rather than silently skipping — the failure mode that
    let #354 (a PAT that cannot POST /check-runs) ship green. Without both, the 12 REAL `gh`
    classifications above would be as inert as the mock they replaced.

    The read seams must also run on EVERY CI path, not just same-repo PRs: the two writes gate on
    `requires_gh_write` / PB_GH_ALLOW_WRITES (set only where GITHUB_TOKEN is write-capable) so the
    read seams still run on fork PRs and pushes. Without that split the whole job had to be gated
    off wherever a write was impossible — which is how the tier was BYPASSED for fork PRs and
    unpinned pushes, leaving these REAL classifications uncovered on those paths."""
    tier = (_ROOT / "tests" / "test_worktree_gh.py").read_text()
    assert "PB_REQUIRE_GH" in tier
    assert re.search(r"requires_gh\s*=\s*pytest\.mark\.skipif", tier)
    # Reads run everywhere; only the writes gate on a write-capable token. This split is what lets
    # the CI job run unconditionally instead of being bypassed where writes are impossible.
    assert re.search(r"requires_gh_write\s*=\s*pytest\.mark\.skipif", tier), (
        "the two write seams must gate on a separate `requires_gh_write` skipif so the read seams "
        "still run on fork PRs / pushes (a read-only token) rather than bypassing the whole tier"
    )
    assert "PB_GH_ALLOW_WRITES" in tier
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "PB_REQUIRE_GH" in ci, "CI must set PB_REQUIRE_GH=1 on the real-GitHub job (r2)"
    assert "PB_GH_ALLOW_WRITES" in ci, (
        "CI must gate the write seams behind PB_GH_ALLOW_WRITES so the read seams run on every path "
        "(fork PRs / pushes) instead of the whole real-GitHub job being bypassed there"
    )
