# Card lifecycle — lanes, review sub-states, and blocked cards

The board's lanes are the easy half and the README shows them in one line:

```
backlog → ready → in_progress → in_review → done
```

What that line hides is where cards actually get stuck: `in_review` is not one state but a
small machine of its own, and `blocked` is a flag with a class, a retry budget and an
escalation path. This file documents both, because every one of the repairs below exists
for a card that stopped moving and gave no clue why.

## Terminal states

`done` and `cancelled` are both terminal and deliberately distinct — `cancelled` keeps a
bad card and its history visible instead of pretending it shipped. `blocked` is a **flag,
not a lane**: the card keeps its underlying state, stays on the board with its reason
visible, and is skipped by the puller until cleared.

## The review sub-state machine

When `review_gate` is on, an `in_review` card carries exactly one review sub-state as a
bead label:

| Label | Means |
|---|---|
| `review-pending` | the adversarial review is running, or was interrupted and the PR reconcile will finish it |
| `changes-requested` | the review bounced the card back to the coder with findings, which ride the requeue so the board shows WHY |
| `review-clean` | the POSITIVE record that the gate ran and found nothing blocking |

`review-clean` is required by the auto-merge edge. Its **absence is not proof of a
review** — an inert or unrunnable gate also clears `review-pending` and lapses to advisory
(#181) — which is exactly why the merge edge cannot key off "no `review-pending`". Any
requeue re-enters via `review-pending`, which drops `review-clean`.

`merge-hold` is separate: the operator's per-card veto on auto-merge, for a green,
reviewed PR they still want to QA by hand. **The loop never sets or clears it.**

### Verdicts are pinned to a head, not to a card

A verdict means nothing without the commit it was made about, so three labels stamp a
short sha:

| Label | What its sha identifies | Lifetime |
|---|---|---|
| `reviewed-head:<sha>` | the PR head a **blocking** verdict was made about | a clean verdict deliberately clears it, so a LATER `changes-requested` can't be judged stale |
| `review-clean-sha:<sha>` | the PR head a **clean** verdict examined (#323) | written only alongside `review-clean`; any requeue drops it |
| `merged-verified:<sha>` | the `origin/<base>` commit the gate ran against (#131) | REPLACED, never accumulated, on each re-verify |

Read the last one carefully: its sha is the **base**, not the head. The currency check is
`label sha == current origin/<base>` — if base has moved the verdict is **stale, which is
not the same as failing**. Staleness alone never blocks; only a gate *failure* on the
merged state does.

> **The 50-char trap.** beads caps a label at 50 characters and **refuses the whole `br
> update` past it** — not a degraded write, a failed one that blocks the card. That is why
> every sha here is abbreviated to `SHORT_SHA_LEN = 12` (git's own abbreviation width).
> `review-clean-sha:` is a 17-character prefix; a full 40-char sha makes 57, and shipping
> exactly that was #353 — green tests, a pin that could never be written. `verified:` plus
> a full sha is 49, one under the cap. **A single character added to any of these prefixes
> is the next #353.**

### The four edges that unstick a review

The gate itself re-runs only on `review-pending`, and auto-merge requires `review-clean`.
So a card whose sub-state is wrong sits in review forever. Four repairs exist, and the
reconcile runs them **in this order** — the ordering is load-bearing:

1. **Re-arm on an external push (#328).** A direct or human push to the branch of a
   `changes-requested` PR moves the head out from under a verdict the gate will never
   revisit. Left alone the stale rejection pins a dead head forever — or, if someone clears
   the labels by hand, an un-reviewed head merges. On a demonstrable
   `reviewed-head` ↔ live-head mismatch the card flips back to `review-pending` and gets a
   fresh review for the new head. **Fails closed**: anything unreadable or ambiguous leaves
   `changes-requested` in place, so the merge edge still cannot touch an un-reviewed head.
2. **Trusted current-head QA PASS (#323).** A promoted QA pass for the PR's *current* head
   repairs a stale `changes-requested` (or an absent verdict) to `review-clean`. Runs after
   #328 so a genuinely moved head takes the fresh-review path instead of this trust path.
3. **Stranded fix round (#340).** A shutdown mid-transition can leave a card `in_review` +
   `changes-requested` with the gate's requeue never landed: no live drive, no way back.
   The trigger here is **liveness, not head identity** — a `changes-requested` card with no
   surviving drive, claimed worktree, or in-flight gate is stranded, and is requeued to
   `ready` with its PR, findings and review-fix budget intact. It invents no new verdict,
   spends no budget, and is idempotent across repeated sweeps.
4. **Merged-verify exhaustion (ADR 0326).** When a sibling merge keeps moving base under an
   in_review PR, the loop re-verifies against the merged state until the budget runs out,
   then **holds** auto-merge rather than merging unverified. `board_reset_merged_verify_budget`
   is the supported way to release one card.

An unchanged head that was genuinely rejected stays rejected. That is the point of pinning
identity to a sha rather than to a timestamp or the presence of a label.

## Blocked cards — self-heal, then page a human

`blocked` carries a **class**, on a `blocked-class:<cls>` label, and the class decides what
happens next. Every sweep, the loop walks the blocked lane:

- **Self-healing classes** — `rate-limit`, `transient`, `merge-conflict` — are cleared and
  requeued automatically, up to **2 auto-retries** per card. These are conditions that
  routinely pass on their own.
- **Everything else, and any card that has spent its retries**, escalates: the operator is
  told **once**, by name, with the real reason. The card stays blocked. A human decides.

The reason lives in a bead *comment*, and `br list` carries none — so a list row always
projects an empty reason. The escalating card is deliberately re-read through `br show`
first, because "no reason recorded" tells the operator nothing and sends them digging,
which is the thing the alert exists to prevent.

### Why the alert doesn't repeat, and when it should

Deduplication is carried entirely by the **key**, not by state on the board's side.
`dedup_key` encodes the incident's identity — the card, its failure class and reason, and
**the recovery cycle it is on**. So the same block dedups by construction, a genuinely
different block is a different key and alerts, and there is no label, memo, generation
counter or rollback to go stale. Earlier cuts tried each of those and review found a
narrower race in every one.

The recovery cycle belongs in that identity: a card that auto-healed, rebuilt, and failed
**the same way again** is a new failed cycle and *is* news, because the self-heal did not
work. Keying on class and reason alone silently suppressed exactly that.

The suppression window is a week, not the inbox's 300s default, because a blocked card can
sit for hours and the short window re-alerted on every restart (#341).

Delivery is **feature-detected**: the operator inbox is a host module the plugin must not
hard-depend on. On a host without it — or if the inbox refuses — the block is still logged
as a WARNING, which is strictly louder than the silence a block used to leave.

## Where to look next

- [`docs/configuration.md`](configuration.md) — `review_gate`, `review_dispatch`,
  `review_fix_max`, `review_run_max`, `auto_merge`, `merged_verify_max`.
- [`docs/tools.md`](tools.md) — `board_block_feature`, `board_unblock_feature`,
  `board_requeue_feature`, `board_reset_merged_verify_budget`.
- [`docs/adr/0326-merged-verify-exhaustion-auto-merge-hold.md`](adr/0326-merged-verify-exhaustion-auto-merge-hold.md).
