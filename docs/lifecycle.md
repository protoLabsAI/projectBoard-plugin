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

### A blocking finding must quote the diff it claims to have read

ADR 0077 requires a finding's `evidence` to quote the diff **verbatim**, and says a quote
that cannot be grounded against the file "loses the power to block". The gate enforces
that: before a `blocker`/`major` finding bounces a card, every substantial line of its
evidence must actually appear in the PR diff.

A finding that fails grounding is **demoted to non-blocking, never dropped** — it is
logged as a warning and written to the bead so a human still sees it. Nothing about the
grounded path changes: a real quote bounces exactly as before.

The guard deliberately fails **open** — it keeps today's blocking behaviour — wherever a
demotion would be a guess rather than a finding:

| Situation | Why it still blocks |
|---|---|
| the diff could not be read, or is empty | nothing to check the quote against |
| the diff came back truncated | a fragment cannot disprove a quote |
| the evidence is empty, or has no line long enough to carry signal | ADR 0077's own out is "cite `file:line` and describe the scenario" — no quote is not a mangled quote |
| the finding names a file the diff never touches | it legitimately quotes a file `gh pr diff` does not carry |
| the finding's `category` is `cross-file` | its whole point is a file the PR did *not* change: it names the changed file but quotes the untouched one |
| an evidence line is an elision (`…`, `...`) | ADR 0077 forbids stitching separate lines into one, so eliding between two real hunks is the honest quote — the elision is the finder's annotation, not part of it |

So the only demotion is a quote with real content that a complete diff of a file the PR
*did* change demonstrably does not contain. Re-indentation and re-wrapping survive
grounding; a changed literal does not.

**Every** substantial line must match, not merely one — #3306's mangled quote had two of
its three lines verbatim, so an "any line matches" rule would have passed it straight
through. Matching is substring rather than line equality, because a quote legitimately
clips a line mid-way; erring toward "grounded" keeps a finding blocking, which is the
safe direction for a guard that only ever demotes.

A demoted finding is re-stamped `uncertain` — ADR 0077's own word for a quote that
cannot be grounded — before the round's findings are carried into the next run's delta
review, so the re-review is not handed back the `confirmed` verdict this round declined
to act on.

Why it exists (protoAgent#3306): a finding quoted `secret = "[REDACTED]"` from a test
whose actual line was `secret = "sk-ABCDEF…"`, concluded the test asserted a
contradiction, and blocked. The coder cannot fix code that already says the right thing,
so the card burned both bounces and terminal-blocked with a green branch discarded. An
ungrounded finding is unfalsifiable by construction, whatever mangled it.

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

## The requirement ledger, and the two ways it can be unsatisfied

A card decomposed at `mark_ready` carries a ledger of requirement items. Before its PR
opens, the coder must dispose of every one — a `## Requirements` section with `- <id>:
done` or `- <id>: declined — <reason>` per item. Silence is not a disposition.

A reply can leave the ledger unsatisfied in two very different ways, and the gate treats
them differently (#382):

| What came back | What it means | What the gate does |
|---|---|---|
| a `## Requirements` section, items still open | a genuine **unmet requirement** — the coder engaged with the ledger | the ordinary `req-fix` bounce: re-dispatch with the open items, then escalate the tier, then block |
| **no `## Requirements` section at all** | a **protocol miss** — the coder forgot the section | one ledger-only follow-up: ask for the ledger *alone*, implementation untouched, same tier, `req-fix` unspent |

The split matters because escalating the model ladder cannot fix a missing markdown
heading. `bd-neiz` spent seven ACP sessions and ~40 minutes climbing `reasoning → opus`
on a card whose implementation had **already passed its acceptance tests**, then
terminal-blocked and reaped the worktree — the whole diff lost to a formatting omission.
It is the same shape as a repeated timeout (#143, #378): there is no error text to fold
into the retry, so attempt N+1 gets a byte-identical instruction and misses identically.

The follow-up is bounded by `_LEDGER_ONLY_MAX` (one). On exhaustion it falls through to
the ordinary `req-fix` bounce — a protocol miss adds no new terminal edge, it only gets
one cheap chance to be a slip rather than a capability failure. Like its sibling pre-PR
budgets it re-arms on a tier climb and on a passed gate, so a card that later returns for
an unrelated reason still gets one.

The follow-up asks for the `## Summary` section back alongside the ledger, which matters
more than it looks: the **goal gate runs before the requirement gate** and re-reads the
coder's reply for a `NO_TEST_NEEDED:` declaration. A ledger-only round that dropped the
summary would fail goal-verify and be told to add a test — inverting the fix and undoing
work the card had already banked. The PR body is built from that section too.

## A killed gate is not a failed gate

The pre-PR and merged-state gates both run a repo's own command. When that command dies on
a **signal** — the member shutting down, an operator `kill`, a worktree reaped underneath
it, the OOM killer — it reached no verdict, so it must not produce one. Like a gate
timeout, it degrades to a pass; CI is still the real gate.

Two forms are read, and both are needed:

| Return code | Means |
|---|---|
| `-N` | the gate process itself died on signal N |
| `128 + N` | a **wrapper** reporting that its child died on signal N — the shell's own convention |

The wrapper form is the one that actually bites. Most gate commands are a script
(protoAgent's `scripts/gate.py`, a `make check`) that runs the real tool as its own child,
so the signal lands one level down and the wrapper chooses its own exit code. A wrapper
that flattens everything to `1` makes a killed gate byte-identical to a red one, and no
guard downstream can recover the difference — which is exactly how #386 blocked a card for
~40 minutes as *"the RESULT is broken"* against a merged state that was fully green.

Ambiguity resolves toward "killed" on purpose: the two errors are not symmetric. Calling a
killed gate red states something false about the code and stops the board; calling a red
gate killed only re-runs it, and the genuine failure is still there on the next run.

### A gate is a process tree, and it dies as one

The board runs every repo command — the pre-PR and merged-state gates, the preflight,
`coder.solve()`'s acceptance tests and the fixups — with **no stdin** and in its **own
process group**. A timeout or a cancel SIGKILLs the whole group, then reaps it on a bound.
The board's `git` and `gh` helpers get the same no-stdin, own-group treatment and kill the
tree on a timeout or cancel (#423).

Both halves are load-bearing. `pnpm install && pnpm run ci` is three processes, not one, and
killing only the shell used to orphan the rest. In the desktop app, every gate child also
inherited the server's stdin: one pipe, shared by every sidecar, that never closes. Hung
`pnpm install`s piled up across two boards, the oldest for 19 hours. One drive went silent
for 8 hours, because on Python ≥ 3.11 `await proc.wait()` does not return until the orphan
closes the stdout pipe it inherited.

This is the host's own contract for process trees it owns (protoAgent ADR 0098), and it
carries the host's trade-off: a member stopped by a signal to its process group no longer
takes a running gate with it. The drive's cancel path kills the tree if shutdown reaches it.
On Windows `killpg` does not exist, so a timeout there still kills only the shell.

## A worktree holding work is saved before it is removed

Worktrees are disposable by design: a re-dispatch cleans a prior run's leftovers with
`git worktree remove --force` and `git branch -D`, and the terminal edges reap by feature
id. That is exactly wrong for one kind of leftover. A coder that dies before its candidate
is promoted leaves the **only copy** of its work in that tree. `bd-ezs7`'s coder finished
170 lines in `feat-bd-ezs7.g1` and its drive went silent (the #423 hang). The card was
requeued, and the implementation survived only because the hung drive still held its file
claim, so nothing re-dispatched it (#400, #405).

So every edge that ends a tree first saves any **work that exists nowhere else** to a new
branch, `stranded/<tree dir>/<UTC stamp>`, and removes the tree only after that. All of
them go through one save-then-remove, locked per tree path, so two edges reaching the same
tree at once (an operator cancel's reap and the cancelled drive, say) produce one save and
one removal:

| Edge | What it does with such a tree |
|---|---|
| a fresh build of the card | saves every one the card owns, removes them, comments on the card, and builds on |
| a drive's terminal block, or an operator cancel | saves the drive's own tree, removes it, and says so on the card |
| shutdown | saves the interrupted drive's tree, comments on the card, and reaps it. The next boot rebuilds as before. |
| the by-id reap: merge, closed PR, cancel, done, health sweep | saves it, logs the branch, and reaps it |
| `create_worktree` / `promote_worktree`, for any other caller | saves it, logs the branch, then clears it |

**What counts as work:**

- **Every uncommitted change git can see:** modified, staged, deleted and untracked.
- **Every commit the tree's `HEAD` or its branch holds that no other branch, tag or remote
  does.** That includes the card's own branch, where the verified candidate is committed
  before `open_pr` pushes it, and a detached `HEAD`.
- **Commits whose content is already published don't count.** A rebase force-pushes
  rewritten commits and a squash-merge lands them under a new one, so git alone would call
  both unique. The check is by content: if merging them into `origin/<branch>`,
  `origin/<base>` or `origin/HEAD` would change nothing, they are published. Auto-merge
  reaps the tree *before* it deletes the PR branch, while that ref still exists to prove it.
- **The board's own droppings don't count, and are never saved:** the coder's session
  scratch (`.proto/`, `.cursor/`) and the `node_modules` links it adds to every tree. A
  `node_modules/` ignore pattern matches only a real directory, so git reports those links
  as untracked. For the same reason, the staging step behind every PR commit now leaves the
  links out. Before this, a repo with only that pattern got the board's symlink committed
  into its PR.

**How the save is made:**

- The saved commit is the tree's own `HEAD` plus its working state, staged through the same
  exclusions a PR uses into a private index. If the branch holds commits a detached `HEAD`
  does not, the save gets a second parent, so both histories survive.
- The tree, its index and its branch are never touched. Every step runs with the repo's
  hooks off (`core.hooksPath=/dev/null`); `update-ref` would otherwise fire
  `reference-transaction`. Nothing is signed, and the identity is pinned.
- The branch name carries the tree's directory and a millisecond stamp. A taken name is
  retried with a `-2`, `-3`… suffix, so an existing branch is never overwritten and a save
  never fails on our own naming.
- The branch is read back and compared with the tree state before the tree may go.
- Once the work is on its branch, a tree git refuses to remove (a read-only directory, say)
  is deleted anyway. A tree the operator **locked** is never deleted.

The card comment names each branch, what it holds, whether the tree really left its path,
and how to use it: `git diff origin/<base>...stranded/…` to inspect it,
`git cherry-pick origin/<base>..stranded/…` or a PR from the branch to salvage it, and
`git branch -D` once nobody needs it. `git branch --list 'stranded/*'` shows what has piled up.

A drive still throws away what **it** built and judged, and saves nothing for it: its own
failed attempt before a retry, and the candidates `coder.solve` or Max-Mode rejected. The
operator-only test-rung diagnostic owns and reaps its own `feat-<id>.test…` trees, so a
card's build never touches them.

### When the work cannot be saved

Most failures keep the tree **exactly as it is**, because work that could not be saved is
never destroyed:

- **A nested git repository.** A branch could hold it only as a pointer, not its files or
  history.
- **A commit or branch step that fails.**
- **A saved tree that will not come off its path**, for example because it is locked.

A **husk** is different: a tree whose admin entry is gone, usually because a removal was
interrupted, so git cannot read it at all. It is moved aside, bytes intact, to
`<worktrees root>/.stranded/<dir>-<stamp>`, and its branch's unique commits are saved. A
husk never blocks a card forever.

Where a failure happens decides what it stops:

- **A fresh build** blocks the card under `stranded-work`. That class is not self-healing,
  so the blocked sweep tells the operator once. The reason names the tree, what is in it
  and what went wrong, plus both ways out: recover it (switch the tree to a branch of your
  own and commit, or open a PR from it) or discard it (`chmod -R u+w <path> && rm -rf
  <path> && git worktree prune`, then `git branch -D <branch>`). The discard works on a
  husk too. Then unblock the card. A card unblocked with the tree still unsavable just
  blocks again.
- **The by-id reap** keeps the tree and logs it once.
- **A drive's block, a cancel, or shutdown** keeps the tree and says so on the card. The
  card's next dispatch tries again.
- **`create_worktree` / `promote_worktree`** refuse with `StrandedWorkError` before anything
  moves.

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

## Attaching a PR the board didn't open

The loop adopts PRs it did not see opened in exactly one place: crash recovery finds the PR
whose head is a card's own branch and moves the card to `in_review`. A PR an operator opens
by hand needs the same thing, for example recovered work pushed from a dead coder's worktree.
`board_attach_pr` / `POST /features/{fid}/attach-pr` (#402) is that edge, and it is no wider
than recovery.

| The PR must be | Because |
|---|---|
| **open**, in the card's project repo, not from a fork | fix rounds push to this repo's branch; a merged PR has nothing left to review (use the manual Done edge) |
| on the card's own branch, `feat/<id>-<slug>` | every later edge keys on it: CI and review fix rounds resume `origin/<that branch>`, and so do recovery and the reap. A PR on any other branch would be abandoned by the first fix round, which opens a second PR |
| targeting the project's base branch | the rebase and merge edges work against that base |

A draft is fine. It attaches like any other PR, and the auto-merge edge holds it until it is
marked ready.

The card must be a coding **feature** (not a task, epic or milestone) that has passed the
Ready gate. That is judged on its lane *underneath* any block: `ready` or `in_progress`,
blocked or not. A backlog card that was merely blocked has not passed the gate. The card must
have no open dependency, and the loop must not be working it: no live drive, no claimed
build, no review gate running.

The board tracks one PR per card:
- A card already in review on *this* PR is a no-op.
- A card that tracks this PR anywhere else is refused.
  - A **blocked** card could be blocked by the review gate asking for a human, and
    re-attaching would lift the block and re-arm the gate without one. Unblocking is a
    deliberate `board_unblock_feature`.
  - A card in a fix round is already being driven back to review.
- A card whose earlier PR was **closed** (rejected, then reworked on the same branch) can take
  the new PR in its place. An earlier PR that is still open or already merged is refused.

Every refusal changes nothing and says what to do instead.

**The write.** It is one `br update` under the loop's claim lock, so a `ready` card cannot be
claimed halfway through.
- It leaves the card where `open_review` would: `in_review` with the PR on `external_ref`.
- It drops `ready`, the `blocked` flag and its class, and every verdict pinned to an earlier
  head: the review verdict and its sha pin, the reviewed-head stamp, and the
  `merged-verified` stamp. The board has never gated or reviewed the attached head, and no
  stamp may say otherwise.
- When `review_gate` is on it sets `review-pending`, so the gate reviews the attached head
  instead of the merge edge waiting forever for a verdict.
- The health sweep's own moves take the same lock and re-read the card first. A sweep that
  read the card before the attach can't requeue it afterwards. If the sweep's recovery adopts
  the same PR first, the attach still arms the gate and records itself.

**The attached code has NOT been through the board's pre-PR checks.** The drive runs fixups,
the local gate and the acceptance tests before it opens a PR; none of those ran on this code.
What still applies is everything after the PR: CI, the review gate when it is on, rebase,
merged-state verification, then merge → `done`.

**Audit.** The attach is an `attached PR:` comment on the card, naming who attached it, the
state it left, and why. That comment is written last. If it fails, the attach still stands
and the result carries a `warning`.

Fix budgets the card already spent are not reset. A card whose automated fix rounds are
exhausted still stops at the next failure, for a human.

## Where to look next

- [`docs/configuration.md`](configuration.md) — `review_gate`, `review_dispatch`,
  `review_fix_max`, `review_run_max`, `auto_merge`, `merged_verify_max`.
- [`docs/tools.md`](tools.md) — `board_block_feature`, `board_unblock_feature`,
  `board_requeue_feature`, `board_reset_merged_verify_budget`.
- [`docs/adr/0326-merged-verify-exhaustion-auto-merge-hold.md`](adr/0326-merged-verify-exhaustion-auto-merge-hold.md).
