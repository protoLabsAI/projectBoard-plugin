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

## A card moved under its build (#398)

A drive owns its card only while the card is `in_progress`. Someone else can move it on
while the coder works: a human hold (which shows as `blocked`), `mark_done`, a cancel, or a
requeue. The move is the newer decision. So before every edge that would change the card,
the drive re-reads it: blocking it, starting another attempt, opening the PR, handing it to
review. If the card is no longer its own, the drive **stands aside**:

- **Nothing is blocked, retried or sent to review.** A failure the build hit afterwards is
  recorded in the trail comment, not in a block that would overwrite the hold or the requeue.
- **The work is kept.** If the card is held, marked done or taken into review elsewhere
  before the PR opens, the build opens none, and the work stays unpushed in its worktree. A
  requeued card is still published for. If a PR did open, it is recorded on the card with
  its state untouched, so the next round resumes that branch instead of rebuilding off base
  over it. Nothing removes that tree without saving it first: when the reap or the card's
  next fresh build ends it, the unpushed work goes to a `stranded/…` branch like any other
  (see the next section). Shutdown leaves it alone, because the drive no longer holds it.
- **The drive's fix budgets reset,** so whoever moved the card starts the next round fresh.
- **Every write is best-effort,** the trail comment included. A failing comment can't turn
  into a block.
- **A cancel** takes the #211 edge, which also closes a PR the build opened.

When the card can't be read, the drive does **not** assume it is still its own. A refused
hand-off names the state it found (`expects in_progress, got 'ready'`), and that answer is
used. With nothing to go on, the drive stands aside, and the health sweep reconciles the
card as one with no live drive.

The commonest trigger is closed at the door. `board_requeue_feature`, `board_requeue_ci_fix`,
`POST …/ci` and `POST …/review` refuse a card the loop is still working (a live drive, a
claimed build, a running review gate), and say to wait for the round or cancel the card.
Live, `bd-p8ft` was requeued under its own CI-fix round. The round's hand-off then failed,
and the card went terminal with an open PR.

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
| a drive's terminal block, or an operator cancel | saves the drive's own tree, removes it, and says so on the card. A drive whose card moved on under it (#398) blocks nothing and removes nothing: the tree stays until another edge here ends it |
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
failed attempt before a retry, and the candidates `coder.solve` or Max-Mode rejected. It
throws away a failed attempt only while the card is still its own, re-read just before. A
card moved on under the drive keeps that tree like any other stand-aside (#398). The
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

### Publishing a stranded tree without a coder

`board_salvage_feature` / `POST /features/{fid}/salvage` publishes a stranded card's
worktree on the board's own machinery, with no coder dispatched (#427). It commits what the
tree holds, runs the pre-PR gate, pushes the branch, opens the PR and moves the card to
`in_review`. Without it, recovering bd-ezs7's finished work needed a coder, and that day
the coder delegate was down for an unrelated reason.

A build that stood aside from a held card (#398) leaves exactly this shape: the card is
blocked, and the finished work sits unpushed in its tree. The salvage publishes it as it
stands. Lifting the hold instead hands the card back to the queue, and its next build
saves that tree to a `stranded/…` branch and rebuilds from scratch.

It is an **operator override**. It skips the checks a drive makes before its PR: the goal
check, the requirement ledger and the source-issue check. CI still gates the PR, and so
does the review gate when `review_gate` is on.

- **Only a stranded card:** `in_progress` with no live drive, or `blocked`. A `ready`
  card could be claimed by the loop mid-publish, so block it first. A card blocked out of
  `in_review` (CI-fix rounds spent, or a review-gate block) keeps its PR: the salvage
  pushes onto that PR and returns the card to `in_review`.
- **Refuses, changing nothing,** while a drive or another salvage owns the card, or when no
  worktree has changes against base. It also refuses when several worktrees do and `tree`
  (a `feat-…` directory name or a path) does not pick one. A directory under the card's
  tree names that the repo never registered as a worktree is left alone. That covers a
  leftover with no `.git`, a husk, or a separate clone. Git run inside a leftover answers
  for the main checkout.
- **The gate runs where the tree stands.** The repo's `format_cmd` fixups run first, as
  in a drive, so they may rewrite files in the tree even when the gate then refuses.
- **A red gate publishes nothing** and returns `gate-red` with the output's tail.
  `force=true` publishes anyway, marked as a draft:
  - A new PR is opened as a **draft** whose body carries the output. Auto-merge never
    merges a draft.
  - A PR the card already has is converted to a draft (`gh pr ready --undo`), and the
    output is posted on it as a comment.
  - `draft` in the record is read back from GitHub. If GitHub refuses the conversion, the
    record and the card comment say the PR is **not** a draft.
  - In a repo without draft PRs, no PR is opened. The branch is already pushed, and the
    record names it with the `gh pr create` command to open one by hand.
- A candidate tree is promoted to the card's own branch first, so every edge after it
  works as it would for a drive's PR. With `review_gate` on, the salvage runs the review
  gate itself, as a drive does. The reconcile only picks up `review-pending` when
  `merge_poll` is on.
- **A cancel mid-publish** stops it before the PR, or closes the PR it has just opened.
  The cancel's reap waits for the salvage to finish, then saves and removes the tree as
  usual.
- While it runs, the card is reserved like a drive's claim, and its trees are held. The
  claim scan, boot recovery, the sweeps, auto-unblock and reaps all leave it alone. The
  reservation is released only by the salvage that made it.

**A hung drive cannot be salvaged in place.** The salvage refuses while a live drive owns
the card. No verb stops a drive and leaves the card as it is. The only ways out are
cancelling the card, which ends it, or restarting the host, after which boot recovery
requeues it. Both save the tree to a `stranded/…` branch and remove it, and the card
comment names that branch. The salvage then has no tree to publish, so publish the branch
by hand: `git push origin stranded/…:<card branch>`, then open its PR. After a restart,
block the card first, or a fresh drive may build over it.

## Blocked cards — self-heal, then page a human

`blocked` carries a **class**, on a `blocked-class:<cls>` label, and the class decides what
happens next. Every sweep, the loop walks the blocked lane:

- **Self-healing classes** — `rate-limit`, `transient`, `merge-conflict` — are cleared and
  requeued automatically, up to **2 auto-retries** per card. These are conditions that
  routinely pass on their own.
- **Everything else, and any card that has spent its retries**, escalates: the operator is
  told **once**, by name, with the real reason. The card stays blocked. A human decides.

A block set **by hand** (`board_block_feature`, `POST …/block`) is always `terminal`, so it
is never cleared automatically. The self-heal also **never moves a card blocked while still
in backlog**, whatever its class, because its requeue would promote a card that never passed
the Ready gate. Only people and agents block backlog cards (the loop blocks only ready and
in-flight ones). A hand block written before this change still carries whatever class its
wording guessed, and it goes to a human instead. The class of a loop-set block is inferred from its reason
by the coder-failure classifier. That classifier reads prose as if it were an error message:
a PM's "waiting on the network team" matched `network`, came out `transient`, and the sweep
cleared the hold and requeued the card to `ready`, straight past the Ready gate. A human's
block is a decision. Only its author knows when it is over (#406).

**The reason is on every read.** It lives in a bead *comment*, and `br list` omits
comments. Until #416 every list row therefore showed an empty `blocked_reason`: in
`GET /features`, in `board_list`, and in the sweep's own read. Cards read as terminal with
no reason while the reason sat one `br show` away. The listing now carries the comment
thread across for blocked rows, from the batch `br show` it already makes for dependencies,
so a blocked card's reason shows wherever the card does. The escalation path still re-reads
a card through `br show` if its reason is somehow empty, because "no reason recorded" tells
the operator nothing. A terminal block can no longer be written without a reason at all
(#414).

### Cards stranded outside the ready lane (#406)

The loop claims only `ready` cards, and only `ready` + `depends_on` is re-checked when a
dependency closes (the dag gate releases it by itself). A card left in **backlog** to wait
for its dependencies, or **blocked** in backlog for the same reason, is never looked at
again once they close. It is not a claim candidate and it shows up in no skip diagnostic.
So the board now names it, wherever a card's next action is shown: the listing, the
console chip, the agent's working state (which names a backlog card only when it owes a step,
and ranks it after every in-flight card so a pile of stranded cards can't push a PR awaiting
merge out of the capped list), and one log line when the loop first sees the card stranded,
on the working-state snapshot's read (held in memory, so a restart logs each stranded card
once more):

- **backlog, every dependency closed** → `dependencies closed — promote`. The step is
  `board_mark_ready`, and the Ready gate still decides. A `deferred` or `designing` card is
  excluded because it is parked for another reason. **`board_mark_designing`** is how the PM
  says so: it parks the card on purpose, and `board_mark_ready` unparks it. "Closed" is
  what beads' dependency gate counts, merged or cancelled. When a dependency was
  **cancelled** (a scope cut, not a delivery), the hint names it and asks to confirm the
  card still makes sense first.
- **blocked in backlog, every dependency closed** → `blocked — dependencies closed`. The
  block may have been only that wait, or it may be unrelated, so it is **surfaced, never
  cleared**. The operator gets one more alert when the last dependency closes.

Nothing is promoted or unblocked for you. A card with no recorded `depends_on` is never
called stranded, because without a recorded edge there is nothing to say has cleared.
Auto-promoting a stranded backlog card is deliberately out of scope: a backlog card may
sit there on purpose, and the Ready gate is a decision point, not a formality.

`board_dispatch` uses the same classification. When nothing is claimable, it no longer
answers a bare `empty-queue` while cards are held. The outcome is `held`, and the record's
`held` field maps each reason to its count, first few ids, and the step that moves it.
The reasons are: `dependencies-closed-promote` and `blocked-dependencies-closed` (the two
stranded shapes); `ready-waiting-on-dependencies` (the dag gate will release these by
itself); `backlog-waiting-on-dependencies`; and `blocked:<class>` for every other blocked
card. `empty-queue` now means nothing is held either.

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
