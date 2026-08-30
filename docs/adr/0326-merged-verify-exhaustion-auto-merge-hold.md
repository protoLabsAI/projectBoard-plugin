# ADR 0326 — Surface merged-verify exhaustion as an explicit auto-merge hold

Status: accepted · Issue: #326 · Supersedes-context: #131 (merged-state verify), #208
(the in_review `next_action` contract), #259 (persisted loop fix budgets).

## Context

`BoardLoop._verify_merged_state` (#131) re-runs the local gate against the *merged*
state (branch tip + current `origin/<base>`) whenever a sibling merge moves base under
an `in_review` PR, and stamps the base sha it verified against as
`merged-verified:<sha>`. The re-verify is bounded by `merged_verify_max` (default 5,
`0` = unlimited): once the budget is spent the loop deliberately **stops** re-verifying
so a base that moves every poll can't burn a gate run forever.

That bound is correct, but on an `auto_merge` board it created a silent trap. The merge
edge (`_auto_merge_blockers`) refuses to merge while the `merged-verified:<sha>` stamp
is stale (≠ current `origin/<base>`). With the re-verify budget spent, the stamp is
never refreshed, so the card can *never* auto-merge as long as base keeps moving — yet
the only signal was a one-time `WARNING` in the loop log. The card sat in review with a
`next_action` of `auto-merge pending`, which is a lie: nothing the loop does will merge
it.

There was also no supported way to un-stick such a card without a host restart: clearing
the persisted `budget:merged-verify:<n>` label by hand does nothing, because
`_budget_get` lets the loop's **in-process cache win over the bead labels** (by design,
#259) — the cached exhausted count still blocks.

## Decision

1. **Reuse the persisted budget sentinel as the exhaustion fact.** When base moves while
   the budget is exactly at the cap, `_verify_merged_state` already writes a one-time
   sentinel — `budget:merged-verify:<max+1>` — via `record_budget`, and logs the
   exhaustion once. That label *is* the loop's durable "base moved while exhausted"
   assertion. A gate-run spend can only ever take the count *to* the cap (the `n >= max`
   guard returns before the gate runs), so `budget > merged_verify_max` uniquely means
   the sentinel was written. No new label, no new loop state.

2. **Project it board-side, config-validated.** `store.merge_posture` /
   `annotate_next_action` gain a new in_review `next_action`,
   `auto-merge held: merged-verify budget exhausted`, emitted only when `auto_merge` is
   on, `merged_verify_max > 0`, and the feature's decoded `merged-verify` budget exceeds
   the *live* cap. It sits inside the existing `auto_merge` branch (replacing
   `auto-merge pending`), strictly behind blocked / merge-hold / the review sub-states /
   draft — so ordinary review, CI-failure, operator-veto, and merge-conflict precedence
   is untouched. The hint names the three remediations: reset the budget, raise
   `merged_verify_max`, or wait for base to stop moving. Because the comparison is
   against the *live* cfg cap, raising `merged_verify_max` flips the projection back to
   `auto-merge pending` with no restart, mirroring how the loop re-arms.

3. **A supported, auditable reset.** A new board tool
   `board_reset_merged_verify_budget(feature_id)` clears **only** the selected feature's
   `merged-verify` budget label (`store.reset_merged_verify_budget`, which records an
   audit comment) *and* invalidates the live loop's in-process cache via a
   process-stable `loop.reset_merged_verify_budget(fid)` (the #178/#211 `sys.modules`
   registry pattern, reload-stable). A blank or unknown feature id alters nothing
   (`_require` raises `BoardNotFound`); the reset touches no other budget kind.

## Consequences

- Budget accounting is unchanged (and now locked by tests): a unit is spent only after
  the merged-state worktree gate actually runs and yields a terminal green (stamp
  written) or failure result; infra errors, merge conflicts, and stamp-write failures
  spend nothing. The exhaustion sentinel is a distinct one-time marker, not a gate-run
  spend.
- Operators see the hold on the card (chip + hint) and the `board_list` / `/features`
  payloads, never only in the log.
- The reset is idempotent and label-scoped; nothing is ever deleted beyond the one
  budget label, and the action is recorded on the bead.
