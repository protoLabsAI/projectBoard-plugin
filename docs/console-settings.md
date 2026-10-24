# Configure it in the console

On protoAgent 0.153.2+, **Settings → Plugins → Project Board → Configure** is split
by operator intent:

- **Coder routing** picks which coders may build (see below).
- **Projects** is the live registry editor. It lists the configured project map and
  can add or update repos only inside the host's enabled Project onboarding root.
  Add/update preserves sibling projects and file-only per-project fields without
  returning those fields' values to the browser, then reads live config back before
  reporting success. Malformed non-mapping entries stay visible but read-only until
  repaired in YAML (the API refuses to overwrite them too). A sole project is the
  runtime's implicit default; adding a second preserves that choice explicitly, and
  the sole default cannot be misleadingly “cleared.” Delete requires explicit
  confirmation and is refused while an active board card names that project—or while
  a legacy unlabelled active card routes through it as the effective default.
- **Automation** contains the loop posture, coder, concurrency/back-pressure, local
  verification, and candidate-generation controls.
- **Review & merge** contains review dispatch/gating, CI reconciliation, rebase,
  dependency release, and auto-merge behavior.
- **Security** contains the webhook HMAC secret. It is stored through the host's
  secret settings path, never returned by the Projects API.

Every scalar is explicit about apply behavior. `coder`, `br_autofetch`,
`max_concurrent`, `max_concurrent_sessions`, `max_pending_reviews`, and `auto_merge`
apply to the running loop; fields marked **restart** are persisted immediately but
do not change the already-constructed loop/router until the member restarts. Project
map/default changes apply live as one validated routing policy and do not produce a
false restart warning.

A save that sets a new local gate command, or moves the project (and its gate) to another
repo, runs that gate once on the clean base before anything persists, and a red gate refuses
the save. That save answers only after the gate has run, which takes minutes for a full test
suite. Saves to other projects don't wait on it. A save that leaves the gate and repo alone
doesn't re-run the gate. That includes a base-branch-only edit: the operator's checkout is
still on the old branch, so a smoke there could give no verdict. Instead, a registry change
resets that project's gate preflight in the loop. The preflight re-smokes the gate against
the new routing before any of the project's work dispatches, and it re-checks cards the
preflight is holding too. If another save changes the same project while a gate runs, the
save is refused with a 409; save again. When a proxy gives up on a long save first (the
fleet proxy allows 20s), the editor reads the outcome from `GET /projects`.

## Coder routing (Configure tab)

**Settings → Plugins → Project Board → Configure → Coder routing** is the console surface
for strict coder routing (`routing.exclusive`): the operator picks which coders may build,
with no YAML. Like Projects, it is a plugin-served page with no config data in it; every
read and save goes through the operator-gated `/api/plugins/project_board/routing` routes.

**Coders.** One card per configured coder shows its provider state (live, or down-marked,
#420), its subscription usage (#579; **Usage unknown** on an older host or with no usage
snapshot) and an **Allowed to build** switch. A switch saves at once through B3's guarded,
audited `PUT /routing`, and the running board uses the change from the next card. Switching
every coder but codex off is "Codex only". With every coder allowed there is no exclusive
set. The last allowed coder can't be switched off. A refused save (a non-operator session,
or a coder that isn't configured or enabled) shows inline and changes nothing, and the page
re-reads afterward so it always shows what landed. Strict routing covers building, CI and
review fixes, and escalation. The review gate and helpers (review dispatch, decompose,
coder.solve offload, Max-Mode judge, task-assignee delegates) keep their own delegates.

**Ladder.** One block per group of projects that route the same way, so a board whose
projects share a ladder shows it once ("All 14 projects"). Each rung (standard / reasoning
/ advanced) lists its coders in order and which one the next card would open on right now.
Hover the "next" pick for why (round-robin, down-marked, `headroom ≥N%`, or exclusive).
That pick comes from `GET /routing/effective`, computed with the loop's own
sibling-preference functions over a read-only snapshot of the round-robin cursor, so it is
a preview: another dispatch or a provider-state change can change it.

Per-project `routing.exclusive` overrides remain YAML/`board_routing_set`-only; a project
with one shows as its own ladder block.

## Not exposed in the console

The console intentionally does not expose every manifest default. Structural legacy
single-repo bindings (`project`, `repo`, `base_branch`, `worktrees_root`, `db_path`)
remain file-only; use the Projects editor for multi-repo routing. Low-level timing and
retry budgets (`review_run_max`, `goal_fix_max`, `local_gate_max`,
`local_gate_timeout_s`, `coder_solve_test_timeout_s`, `coder_solve_budget`,
`coder_solve_k`, `coder_solve_tree_depth`, `loop_interval_s`, `coder_timeout_s`,
`merge_poll_interval_s`, `auto_merge_max`, `merged_verify_max`,
`health_sweep_interval_s`) also remain YAML-only expert tuning. Nested maps such as
`coders`, plus per-project advanced fields not owned by the editor, are preserved but
not flattened into lossy text inputs.

## Setting reference
The labels and help below match Configure. Each heading provides the longer
explanation for that setting; apply and restart behavior is unchanged.
The host's scalar settings do not expose a per-setting documentation link, so this
reference holds the “Learn more” detail.

### Build ready cards automatically (`loop_enabled`)

| Label | Help |
|---|---|
| Build ready cards automatically | Start building ready cards after restart, or turn off to keep the board and tools available without starting builds. |

Start the background puller after the member restarts. Off keeps the board and tools
available without dispatching work.

### Timeouts before asking to split a card (`decompose_after_timeouts`)

| Label | Help |
|---|---|
| Timeouts before asking to split a card | After this many fresh-build timeouts, pause the card and ask to split it into smaller tasks; zero disables the request. |

A repeated coder timeout means the card is too WIDE, not that the model is too weak: it
produces no diff and no CI output, so a retry re-sends the same prompt and a tier climb
spends a stronger model on the wrong problem. After this many timeouts on fresh builds
of one card (a pre-first-token timeout, or one on a fix round, never counts), park the
card and file a task asking this agent to split it into buildable slices (once per
card), with no further tier climb or rebuild. Unblocking a parked card resets its count.
0 turns the ask off and the card simply blocks.

### Coder for builds (`coder`)

| Label | Help |
|---|---|
| Coder for builds | Choose the configured coder that builds cards, with changes taking effect on the next check. |

Name of the acp delegate (Settings ▸ Delegates) the loop dispatches builds to. No
default. Applies live on save: a paused loop resumes on its next check.

### Download the board tool automatically (`br_autofetch`)

| Label | Help |
|---|---|
| Download the board tool automatically | Download and verify the missing board command-line tool from github.com, or turn off to install it yourself. |

When the beads CLI (br) is not on PATH, download the pinned beads-rust release for this
platform into the agent's data dir and use it (sha256-verified; needs github.com). Off =
a missing br is a setup gap with the manual install hint. Applies live on save.

### Cards building at once (`max_concurrent`)

| Label | Help |
|---|---|
| Cards building at once | Limit how many cards build at once; lowering this limit lets running builds finish. |

How many features the loop drives at once, each in its own worktree (1 = serial). Size
it to the board's projects — one slot per repo keeps a deep queue in one project from
starving the others. Applies live on save; lowering it never kills a running build, the
loop just stops claiming until it is under the cap. Peak coder processes = this ×
coder_solve_k.

### Parallel coding sessions per build (`max_concurrent_sessions`)

| Label | Help |
|---|---|
| Parallel coding sessions per build | Limit simultaneous coding sessions within each build; zero allows as many as the candidate budget permits. |

Caps the best-of-k fan-out INSIDE one build (0 = unlimited within the k budget; 1 =
generate candidates one at a time). Applies to the next dispatch. Host-wide peak ACP
processes = Cards building at once × this (bounded by the candidate budget).

### Pull requests waiting for review (`max_pending_reviews`)

| Label | Help |
|---|---|
| Pull requests waiting for review | Pause new builds when this many pull requests await review; zero removes the limit. |

Back-pressure: stop claiming new work while this many PRs already sit in review (0 =
unlimited). Applies live on save. Raise it alongside Cards building at once or the extra slots
idle behind the review queue.

### Check acceptance criteria before review (`goal_verify`)

| Label | Help |
|---|---|
| Check acceptance criteria before review | Have an independent model check the changes against the card’s acceptance criteria, including test coverage, before opening a pull request. |

Before opening a PR, ask an independent model to check the diff against the feature
acceptance criteria, including test coverage.

### Update project checkouts automatically (`base_refresh`)

| Label | Help |
|---|---|
| Update project checkouts automatically | Keep board-owned checkouts up to date, leaving local edits, other branches, and diverged history untouched. |

Each health sweep fetches the base branch of every board-owned checkout (under the
onboarding root, or registered by the board) and fast-forwards it when it is clean and
on the base branch, so the tree the agent reads is current. Your own checkouts elsewhere
are never moved unless their project sets base_refresh: true. A checkout with local
edits, on another branch or diverged is left alone and reported stale on the board
status. Off = no fetch, no fast-forward.

### Checks before opening a pull request (`local_gate_cmd`)

| Label | Help |
|---|---|
| Checks before opening a pull request | Run this check command before opening a pull request unless the project overrides it; blank disables the default checks. |

Fast lint/typecheck/unit command run before a PR opens. Project-specific values in the
Projects tab override it. Blank disables the default gate.

### Try solutions until tests pass (`coder_solve`)

| Label | Help |
|---|---|
| Try solutions until tests pass | Try and test alternative solutions when the coding plugin, acceptance criteria, and a test command are available. |

Use coder.solve's greedy, best-of-k, and tree-search ladder when the coder plugin,
acceptance criteria, and a runnable test command are available.

### Tests for candidate solutions (`coder_solve_test_cmd`)

| Label | Help |
|---|---|
| Tests for candidate solutions | Run this command to test candidate solutions, or leave blank to use the checks configured before opening a pull request. |

Command that verifies generated candidates. Blank falls back to the local gate; still
blank disables the solve ladder.

### Alternative builds to compare (`max_mode_n`)

| Label | Help |
|---|---|
| Alternative builds to compare | Build each fresh card this many ways and keep the best result; one turns off alternatives, while higher values multiply coding cost. |

Build a fresh feature N ways and keep the best passing diff. 1 disables Max-Mode; larger
values multiply coder cost.

### Merge approved pull requests automatically (`auto_merge`)

| Label | Help |
|---|---|
| Merge approved pull requests automatically | Merge pull requests when all required checks and reviews are current and passing, unless the card is held for manual approval. |

Merge an in-review PR once every gate the loop runs is green and current: GitHub CLEAN
(required checks + branch protection), the merged-state verdict stamped against the
current base, and a clean review-gate verdict. Off = park green PRs for you to merge.
Label a card merge-hold to exempt it. Applies live on save.

### Failed checks before holding a merge (`merged_verify_max`)

| Label | Help |
|---|---|
| Failed checks before holding a merge | Hold automatic merging after this many failed or inconclusive checks against the latest base; zero allows unlimited attempts. |

Failed or inconclusive merged-state re-verifications allowed before auto-merge is held.
A green verdict resets the count. 0 = unlimited. Applies live on save.

### How to merge pull requests (`merge_method`)

| Label | Help |
|---|---|
| How to merge pull requests | Use this GitHub merge method when merging pull requests automatically. |

GitHub merge strategy used by auto-merge.

### Require review before merging (`review_gate`)

| Label | Help |
|---|---|
| Require review before merging | Review changes before merging and send serious findings back to the coder to fix. |

Run the configured review workflow before merge and bounce blocker/major findings back
to the coder.

### Workflow for required reviews (`review_workflow`)

| Label | Help |
|---|---|
| Workflow for required reviews | Use this configured workflow to review changes before allowing a merge. |

Host workflow recipe used by the blocking review gate.

### Agent for pull request reviews (`reviewer`)

| Label | Help |
|---|---|
| Agent for pull request reviews | Use this review agent for optional reviews or when required reviews have no workflow runner available. |

A2A reviewer used for advisory dispatch or as the blocking gate's fallback when no
workflow runner is available.

### Request an extra review on opening (`review_dispatch`)

| Label | Help |
|---|---|
| Request an extra review on opening | Ask the review agent for an advisory review when a pull request opens. |

Also dispatch the reviewer delegate when a PR opens. Leave off when the fleet PR-review
pipeline already reviews on open.

### Attempts to fix review findings (`review_fix_max`)

| Label | Help |
|---|---|
| Attempts to fix review findings | Let the coder fix review findings this many times before blocking the card for your attention. |

Same-tier re-dispatches for actionable review findings before the card blocks for an
operator.

### Check pull request status automatically (`merge_poll`)

| Label | Help |
|---|---|
| Check pull request status automatically | Check for merged or closed pull requests so the board stays current even when GitHub cannot send updates. |

Poll in-review PRs so merged and closed state lands even when GitHub cannot reach the
webhook.

### Send failed checks back to the coder (`ci_poll`)

| Label | Help |
|---|---|
| Send failed checks back to the coder | Check pull request test results and send failures back to the coder to fix. |

Poll CI for in-review PRs and re-dispatch failures instead of leaving red PRs parked
forever.

### Attempts to fix failed checks (`ci_fix_max`)

| Label | Help |
|---|---|
| Attempts to fix failed checks | Allow this many automatic fixes for failed checks before blocking the card; zero blocks on the first failure. |

Automatic CI-fix re-dispatches before the card blocks for human triage. 0 blocks on the
first failure.

### Rerun failed checks before changing code (`ci_rerun_max`)

| Label | Help |
|---|---|
| Rerun failed checks before changing code | Retry failed GitHub Actions jobs this many times before asking for a code fix; zero disables retries. |

Rerun a red PR's failed GitHub Actions jobs this many times per head before spending a
CI fix attempt on a possible flake. 0 turns reruns off.

### Update pull requests with base changes (`auto_rebase`)

| Label | Help |
|---|---|
| Update pull requests with base changes | Update pull requests when their base branch changes and ask the coder to resolve conflicts. |

Cleanly rebase behind PRs and re-dispatch genuine conflicts so parallel work remains
mergeable.

### Attempts to fix merge conflicts (`rebase_fix_max`)

| Label | Help |
|---|---|
| Attempts to fix merge conflicts | Let the coder resolve merge conflicts this many times before blocking the card. |

Coder re-dispatches allowed for a genuine rebase conflict before the card blocks.

### When dependent cards can start (`dep_gate`)

| Label | Help |
|---|---|
| When dependent cards can start | Start dependent cards after their prerequisite merges or reaches review; shared foundation changes must always merge first. |

Release non-foundation dependents only after merge (safe) or once the blocker reaches
review (more parallel). Foundation work always waits for merge.

### Secret for GitHub updates (`webhook_secret`)

| Label | Help |
|---|---|
| Secret for GitHub updates | Verify incoming merge, check, and review updates with this secret; blank disables public update routes. |

HMAC-SHA256 secret for public merge, CI, and review ingress. Blank disables every public
mutation route. Stored in secrets.yaml.

### Token for private package checks (`npm_token`)

| Label | Help |
|---|---|
| Token for private package checks | Use this read token to check private npm package releases; blank checks the public registry without signing in. |

Read token for npm publish gates (waits_for npm:...) on PRIVATE packages. Blank reads
the public registry anonymously, which is all a public package needs. Stored in
secrets.yaml; PROJECT_BOARD_NPM_TOKEN or NPM_TOKEN also work.
