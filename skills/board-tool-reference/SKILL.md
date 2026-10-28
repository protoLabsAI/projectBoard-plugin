---
name: board-tool-reference
description: >-
  The board_* tools' full reference, loaded on demand. Use when writing or repairing a card
  (files_to_modify rules, the `(new)` marker, breadth caps, shared files, dedup, source_issue,
  re-homing a card's project, `waits_for` publish-gate grammar), when reading a `next_action`
  / `next_action_hint` or a board_dispatch outcome you don't recognise, or when recovering a
  stuck card (fix round vs CI-fix round, unblock vs override, salvage, attach a hand-pushed PR,
  merged-verify budget, a task's deliver/verify cycle). The tool descriptions carry the rules
  you must obey at call time; this skill carries the detail behind them.
tools:
  - board_create_feature
  - board_create_task
  - board_update_feature
  - board_remove_dependency
  - board_get_feature
  - board_check_ready
  - board_mark_ready
  - board_list
  - board_dispatch
  - board_check_gates
  - board_requeue_feature
  - board_requeue_ci_fix
  - board_unblock_feature
  - board_attach_pr
  - board_salvage_feature
  - board_deliver
  - board_verify
  - board_reset_merged_verify_budget
---

# Board tool reference

The detail behind the `board_*` tool descriptions. The descriptions say what each tool does
and the rules you must follow; this file says how the pieces fit.

## Writing a card

### The Ready gate

`board_mark_ready` (and the `ready_check` dry run that `board_create_feature`,
`board_update_feature` and `board_check_ready` return) refuses a card on any of these,
naming EVERY failure at once with its fix:

- **required-fields**: a coding feature needs a self-sufficient `spec`, testable
  `acceptance_criteria` AND `files_to_modify`. Vague tasks make a coding agent produce
  nothing. A TASK needs only `spec` + `acceptance_criteria`.
- **phantom-paths**: every `files_to_modify` path must exist in the project's repo,
  EXCEPT a file the card will CREATE, which carries `(new)` after its path:
  `src/tokens.js, dist/tokens.css, .changeset/add-accent.md (new)`. A changeset is always
  new, so in a changesets repo nearly every card carries one `(new)` entry.
- **breadth**: more counted files than the cap (small/medium 4, large 6). Files matching
  the project's `breadth_exclude` globs still belong in the list (the coder needs them) but
  don't count. By default: `.changeset/**`, lockfiles, `**/dist/**`, `*.generated.*`,
  `CHANGELOG.md`, `changelog.d/**`.
- **design**: a `large` card needs an ADR-citing `design`.
- **shared-file**: two open cards naming the same file must be ordered by a `depends_on`
  PATH (a chain counts: C → B → A orders A and C too). A project's `hot_files` are chained
  for you at create. `ready_check.suggested_edges` (earliest-created first) lists the edges
  that would serialise every unserialised pair.

`board_check_ready` returns `{id, state, ok, refusals, advisories, breadth, unserialised,
suggested_edges}`. `refusals` items are `{gate, message, fix}`. `advisories` refuse nothing
(e.g. a `depends_on` edge onto a cancelled card). `breadth` is the counted total, the cap,
and the files `breadth_exclude` left out.

The card is created whether or not `ready_check.ok`; fix what it lists, then
`board_mark_ready`. Only `ready` cards are pulled.

### Fields

- `parent`: the epic/milestone id. `priority`: 0 = highest. `difficulty`
  (small|medium|large) seeds the model tier.
- `depends_on`: comma-separated blocker ids on THIS board. On update it ADDS edges;
  `board_remove_dependency` tears them down.
- `foundation=True`: a card others build on. Dependents gate on its MERGE, never its
  review. `board_update_feature` can set it but never clears it.
- `source_issue`: the ORIGINATING GitHub issue, a full URL or `owner/repo#N`. Stored
  normalized as `owner/repo#N` in the bead's notes metadata (a label can't carry `/` or
  `#`). The card's PR gets a `Fixes #N` line, so the issue auto-closes on merge. On update,
  `"none"` clears it: move a split's closing edge to its LAST slice.
- `project`: the entry in the board's `projects:` map the card builds in. It decides which
  repo the worktree/branch/PR target and which repo the Ready gate checks the files
  against; stamped as an immutable `project:<name>` label. Defaults to `default_project`
  (`board_projects` lists the names). A single-repo board can ignore it.
- Every string input has one layer of literal wrapping double quotes stripped first.

### Re-homing a card (`board_update_feature(project=…)`)

For cards left on `default` after a board moved from the flat `repo:` binding to
`projects:` — `board_list` flags them with `project_unresolved: true` and a `project_hint`
naming the call. Allowed only while the card is backlog or ready, has no PR and has never
been dispatched; otherwise refused with the reason. A ready card must also have its
`files_to_modify` in the new repo; fix them in the same call.

### Dedup (create)

`board_create_feature` and `board_create_task` refuse when:

1. a card with the same title is already OPEN (backlog/ready/in_progress/in_review/blocked)
   — calling create twice (e.g. reconsidering mid-turn) stacks a duplicate the loop churns on;
2. `depends_on` names a card already `in_review` with an open PR — work built on in-flight
   work is a fix round on THAT card's PR: `board_requeue_feature(fid, findings=...)`;
3. `source_issue` is set and an open PR in that issue's repo already references the issue
   number — review or contribute to that PR instead of sending a second coder.

`force=true` bypasses all three. A store read or GitHub API failure never blocks creation
(better a possible duplicate than a stuck board).

### Publish gates (`waits_for`)

Comma-separated external facts that must ALL hold before the loop claims the card — for
work that needs something another repo SHIPS, not merely merges. Use `depends_on` for a
card on this board.

- `npm:<package>@contains:<owner>/<repo>@<card-id>` — for a consumer of another card's
  change, e.g. `npm:@protolabsai/ui@contains:protoLabsAI/protoContent@bd-a1`. Met only once
  a published version is proven to contain that card's merge commit. Never use a version
  floor for this; an unrelated publish can satisfy it.
- `npm:<package>@<semver-range>`
- `release:<owner>/<repo>@<tag>`
- `release:<owner>/<repo>@<package>@<range>` (a bare range is refused on a repo that tags
  per package)
- `pr:<owner>/<repo>#<n>` (merged)

A malformed spec refuses the create/update. An unmet gate keeps the card out of the claim
with `next_action` `waiting on publish: …`. On update, `waits_for` REPLACES the gates;
`none` clears them. `board_check_gates` re-checks now (fail closed: a failed check reads
unmet with its `error`; a spec asked within 15 s is answered from cache). The
cross-repo-chain skill walks a whole producer → publish → consumer chain.

## Reading the board

### `board_get_feature` fields

`id, title, spec, acceptance_criteria, design, state, labels, pr_url, merge_hold,
difficulty, files_to_modify, foundation, priority, source_issue, requirements,
work_locations, depends_on, open_depends_on, project, waits_for`, plus when relevant
`recovery_context`, `gates`, `next_action`/`next_action_hint`, `blocked_reason`/
`blocked_class`, `preflight_failure`, and for a task `deliverable`, `delivered_by`,
`open_requirements`.

- `requirements`: the ledger decomposed from the acceptance criteria at mark_ready, each
  `{id, text, status, decline_reason?}` (open/done/declined). The completion gate refuses
  a PR while any item is open.
- `depends_on` is every edge ever (including merged blockers); `open_depends_on` is what
  blocks it now.
- `gates`: each gate's last verdict `{spec, met, detail, error, checked_at}` from the loop's
  cache (no network).
- `preflight_failure` (`blocked_class: preflight-hold`): `{cmd, step, step_index, steps,
  exit_code, headline, tail}` — which step of the project's gate failed on its clean base
  checkout, the line that says what broke, and the last 30 lines (secrets redacted). That
  is an environment fault, not the card's: report it to the operator. The loop lifts the
  hold once the gate passes.

### `next_action` values (`board_list`, `board_get_feature`)

Every `in_review` row carries one, derived from labels + the LIVE config (no network; a
Settings save to `auto_merge` changes it without a restart):

- `awaiting-merge (auto_merge off)` — reviewed, nothing board-side in the way, and the loop
  will NOT merge. Also `awaiting_merge: true` and a hint ("auto_merge is off — merge #N or
  turn it on in Settings ▸ Project Board"). Lead the status report with it; offer merging
  the PR or enabling `auto_merge`, not a re-review (the review gate already cleared it).
- `auto-merge pending` — the loop merges once GitHub reports CLEAN.
- `review in progress`, `changes requested`, `awaiting review verdict (no review-clean)`,
  `merge-hold (operator veto)`, `blocked`, `draft (run gh pr ready)`.
- `ci failing` — with `with_ci=true`, a red rollup replaces the above (never "merge #N" on
  a red PR).
- `auto-merge held: merged-verify budget exhausted` — see below.

A card STRANDED outside the ready lane carries one too, with the verb in its hint:

- `dependencies closed — promote` — a backlog card whose dependencies have all closed.
  Nothing promotes it but you: `board_mark_ready`, or `board_mark_designing` if it is
  parked on purpose.
- `blocked — dependencies closed` — blocked in backlog with all dependencies closed. The
  block is never cleared for you; read its reason before `board_unblock_feature`.
- A dependency that was CANCELLED rather than merged is named in the hint: confirm the card
  still makes sense first.

### `board_list` CI

`with_ci=true` adds `ci_status` (passing|failing|pending|none; "" = no PR probed) and the
failing check names in `ci_summary`. Each PR-bearing row costs a `gh` call, so it is
opt-in. `failing_only=true` implies `with_ci` and keeps red rows; with no `state` it
defaults to `in_review` (the "which PRs are red" query).

### `board_dispatch` outcomes

- `dispatched` — a card was claimed and a drive started (id in `dispatched`).
- `empty-queue` — nothing ready, nothing held.
- `held` — nothing claimable but cards are held; `held` maps each reason to its count,
  first ids and the step that moves it: `dependencies-closed-promote`,
  `blocked-dependencies-closed`, `ready-waiting-on-dependencies`,
  `backlog-waiting-on-dependencies`, `waiting-on-publish`, `blocked:<class>`.
  `blocked:preflight-hold` also carries `preflight_failures` per project.
- `at-capacity` — all `max_concurrent` slots full.
- `review-wip-limit` — `max_pending_reviews` PRs already await review.
- `parked` — a task card was claimed to in_progress awaiting async delivery (no slot held).
- `all-candidates-held` — every ready card was blocked/held, deferred by the hot-file
  guard, held by a project preflight, or lost a claim race (see `skipped`).
- `loop-disabled` (`project_board.loop_enabled=false`), `loop-not-running`, `error` (a
  dispatch stage crashed; the record names the stage and exception).

`detail` is a one-sentence summary; `running`/`max_concurrent` show the slots.

## Recovering a card

| Situation | Verb |
| --- | --- |
| Adverse review findings on an `in_review` PR | `board_requeue_feature(fid, findings=…)` — the fix round on the SAME PR; the next dispatch prompt leads with the findings. `escalate=true` climbs the model ladder (at the top the card blocks instead; no ladder = same tier). |
| Transient failure, retry as-is | `board_requeue_feature(fid)` — back to ready, PR kept, assignee cleared, original prompt. |
| CI bounced the PR (card `in_progress` with an open `pr_url`) | `board_requeue_ci_fix(fid, ci_failure=…)` |
| Stuck on something external | `board_block_feature` → later `board_unblock_feature` |
| Spec wrong | `board_update_feature` |
| Review gate exhausted its fix budget and blocked the PR | `board_unblock_feature` re-arms the gate (fresh review of the live head, fresh budget); `override_review=True` only as a human accepting the findings |
| Worktree holds finished work, no coder should redo it | `board_salvage_feature` |
| Someone pushed a PR by hand for the card | `board_attach_pr` |
| Shipped outside the board | `board_mark_done` |
| Created in error | `board_cancel_feature` |

Both requeue verbs are refused while the loop is still working the card (a live drive or
review gate); wait for the round to end, or cancel. Without `findings`/`ci_failure` there
is no new feedback, so the coder repeats the original prompt.

### Unblock with `override_review=True`

Posts a success `board/review-gate` status at the PR's live head, sets review-clean PINNED
to that head, and records an audit comment. The merged-state / CI / auto-merge gates still
decide the merge, and a later push leaves the pin stale, so an override never covers an
unreviewed head.

### `board_attach_pr`

A card whose earlier PR was CLOSED may take the new one. Re-attaching the card's own PR is a
no-op in review and refused elsewhere (a blocked card stays blocked until
`board_unblock_feature`). A draft attaches; auto-merge holds it until it is marked ready.
The code has NOT been through the board's pre-PR checks (fixups, local gate, acceptance
tests). If the audit comment fails, the attach stands with a `warning`.

### `board_salvage_feature`

A candidate tree (`feat-<id>.g1`) is promoted to the card's branch first. A card blocked
out of review goes back to review on its own PR. `tree` is a `feat-…` directory name or a
path. `force=true` on a red gate publishes a DRAFT carrying the gate output (an existing PR
is converted and the output posted on it). Returns `outcome` (`published` / `gate-red` /
`refused` / `cancelled` / `not-found` / `error` / `loop-not-running`), `detail`,
`worktree`, `branch`, `pr_url`, `draft`, `gate_output`.

### Merged-verify budget

When sibling merges keep moving base under an in_review PR, the loop re-runs the gate
against the merged state and stamps `merged-verified:<sha>`, bounded by
`merged_verify_max`. Once spent, the stamp goes stale and with `auto_merge` on the PR can
never auto-merge. `board_reset_merged_verify_budget` clears both the persisted
`budget:merged-verify:<n>` label (with an audit comment) and the loop's in-process cache,
no restart. Raising `merged_verify_max` in Settings ▸ Project Board is the board-wide fix.

## Tasks (deliverables, not PRs)

A task rides the same rails (ready → claim → in_progress → in_review) but
`board_deliver` moves it to review with no PR and `board_verify` is its Done edge. A coding
feature is never delivered or verified: closing code anywhere but the merge would dodge
`record_merge`, and a feature in review with no `pr_url` would strand the merge reconciler.

- `board_deliver`: `text` is recorded as a `deliverable:` comment; `ref` lands on the same
  `external_ref` slot a feature's pr_url uses. With a requirement ledger, end `text` with:

  ```
  ## Requirements
  - r1: done
  - r2: declined — <reason>
  ```

  Exactly that form. A hedge like `done?`, a reasonless decline, or rows inside a code fence
  close nothing. Unreported items stay open for the verifier. While the task awaits its
  verdict, the rows of a refused or repeated delivery still land.
- `board_verify(approved=false)`: `feedback` becomes the lead of the next dispatch prompt;
  each reopened requirement keeps what it claimed as `reopened_from`.
- `board_verify(by=…)` is forwarded verbatim. Empty is deliberate: the store resolves it to
  its own actor (the HTTP API defaults to `operator` because it's a different caller). A
  verifier matching the deliverer is recorded with a `self-verified` label, never refused.
- Open requirements never refuse approval; the result carries a `note` ("2 requirement(s)
  still open: r2, r4").
