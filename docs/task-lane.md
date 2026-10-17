# The task lane — deliverables, not PRs

Not every board card ships code. A **task-type bead** (`issue_type: task`, #217) rides
the SAME rails as a coding feature — `ready → in_progress → in_review → done` — but its
output is a **deliverable** (a doc, a decision, an artifact ref), not a PR.
`record_delivery` (`board_deliver`) moves it to `in_review` with no `pr_url`, stamping
the deliverable text plus a `delivered-by: <actor>` note — the assignee **at delivery
time**, captured then so a later reassignment can't rewrite who actually delivered it.
A delivery either lands whole or is refused: if a write fails, the card never reaches
`in_review`, and the requirement ledger is only touched after it does. A caller can make
the same call again. The loop can't, so it logs the failure and leaves the card
`in_progress` for the sweep to re-dispatch. Repeating a delivery the card already carries
is a no-op. A **different** deliverable for a task already in review is refused rather
than written over the one awaiting verification. Deliveries of one card are serialized
within the process, so two racing ones can't both land. An **empty** delivery (no text,
no ref) is refused. An agent's empty reply is treated as a failed dispatch, not a
delivery. A dispatch that fails after the agent already delivered in-turn leaves the
delivered card for its verifier instead of blocking it.

A task's acceptance criteria become the same **requirement ledger** a coding feature
gets. A deliverable that ends with a `## Requirements` section — one `- r2: done` or
`- r3: declined — <why>` line per item, the rows a coder reports — closes those items.
The rows must be exact: a hedge like `done?`, or a decline with no reason, closes nothing,
and rows quoted inside a code fence are ignored. While the task awaits its verdict, the
rows of a refused or repeated delivery still land. That covers an agent that delivers its
document in-turn and replies with the section. Nothing lands after the verdict.
A **rejection** reopens every item the rejected round closed, each keeping what it had
claimed (`reopened_from`), and the next round's prompt leads with the rejection feedback.
What is left open is **surfaced, never enforced**: `board_get_feature` lists a task's
`open_requirements`, and a verification's result carries a `note`
("2 requirement(s) still open: r2, r4"). In the **Board** view, the task drawer lists
the ledger above Approve/Reject with each item's status, open ones flagged, and an
approval past open items shows that note in the drawer. Neither the delivery nor the
approval is refused on open items; the verifier decides.

The board listing (`GET /features`) keeps each task row small: `delivered`,
`deliverable_chars`, `delivered_by` and a short whitespace-collapsed
`deliverable_preview`, never the full text. They describe the **current** round:
`delivered` is true only while the task is in review or done. A task sent back from
review lists as not delivered, with its earlier text as `last_deliverable_preview`. The
single-card reads (`GET /features/{id}`, `board_get_feature`) carry the whole latest
deliverable.

Because a task has no PR to merge, its **Done edge is a verifier's approval**, not
`record_merge`: `board_verify` (the agent tool) / `POST …/features/{id}/verify` (the
HTTP API) / `record_verification` (the store) close an approved task with an auditable
`verified: <who>` reason — a deliberate *second* `br close` edge beside the code path's
one Done edge. A rejection instead records the feedback as a comment (the re-dispatch
prompt leads with it) and requeues the bead to `ready` for another pass.

## Self-verification is recorded, not refused

On approval the store compares **who verified** (`by`) against **who delivered** (the
projected `delivered_by`). When they match — the same identity delivered *and* approved
the work, with no second pair of eyes — the close is **flagged, never blocked**: a
`self-verified` label plus a `(self-verified)` note appended to the `verified:` reason.
Refusing a self-verification is deliberately out of scope; the board's posture is to make
it **visible**, not to gate it. An unattributed delivery (no `delivered-by:` stamp and no
assignee) has the store actor stand in as the deliverer, so an actor verifying its own
unattributed task is flagged too. The projection surfaces the outcome as
`self_verified: true` beside `delivered_by` / `verified_by`, and the **Board** view
renders a done, self-verified task with a caution badge on the card and a
`delivered by X, self-verified by Y` provenance line in the drawer.

This is a **provenance flag over the identities the callers supplied**, not an identity
or authentication guarantee: the match casefolds and trims the two recorded strings and
does nothing more. It tells a reviewer that a deliverable closed without an independent
verifier; it does not attest who those actors *really* were, nor authenticate the caller.

## The default verifier splits by caller — on purpose

An omitted `by` resolves differently depending on which door the verification came
through, and the split is intentional (#316):

- **Agent tool** (`board_verify`, `by` omitted) → the **store actor**. The tool *is* the
  agent, so a blank `by` forwards straight through and the store stamps its own actor —
  the agent attributing the close to itself.
- **HTTP / console API** (`POST …/verify`, `by` omitted) → **`operator`**, NOT the store
  actor. A console verification is out-of-band by construction; defaulting it to the store
  actor would falsely flag an **agent-delivered** task, approved by a human in the console,
  as self-verified. `operator` keeps that human-in-the-console close distinct from the
  deliverer.

An explicit `by` is forwarded verbatim through every door.
