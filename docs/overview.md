# How it works

The mechanics behind the board: the store, the loop, the gates and the planning layer. Start with the [README](../README.md) for the five-minute version.

- **Board = a projection over beads** (`.beads/*.db` + git-committed JSONL) — no
  separate store, so the work graph can't drift out of sync. By default the whole
  board — every project's cards — lives in **one store per instance** (see
  `db_path` under Install), never scattered across per-repo `.beads/` workspaces.
- **The loop** pulls the top-priority `ready` feature → creates a disposable
  `git worktree` off `origin/<base>` → dispatches a coder (`acp` delegate) scoped to
  it → commits/pushes → opens a PR → `in_review`. A **merge webhook** sets `done`
  (and reaps the worktree); where GitHub can't reach a webhook URL, a **PR reconcile
  poll** (`merge_poll`, on by default) drives the terminal edges itself — merged →
  `done`, closed-unmerged → `blocked`. Set `max_concurrent > 1` to build several
  features in parallel, each in its own worktree.
- **Resilience** — every `await` in a drive is bounded (a coder dispatch is hard-capped
  by `coder_timeout_s`); **transient** failures (rate-limit / network / merge-conflict)
  retry with backoff while **capability** failures (no diff / timeout) escalate a tier
  or block; and on restart the loop **recovers** features stranded mid-build (adopt an
  already-opened PR → `in_review`, else reset → `ready`). Before it rebuilds over or reaps
  a worktree holding work that exists nowhere else, it saves that work to a `stranded/…`
  branch and says so on the card. Only work it cannot save blocks the card, as
  `stranded-work` ([`lifecycle.md`](lifecycle.md), #405).
- **DAG + gates** — `depends_on` are `blocks` edges; a dependent stays out of the
  puller until its blocker is **merged** (foundation merge-gate). The **Ready gate**
  requires a spec, EARS acceptance criteria, and explicit `files_to_modify` (a file the
  card creates marked `(new)`), within a breadth cap that ignores generated files and
  changesets (`breadth_exclude`), with cards sharing a file ordered by a `depends_on`
  path. It names every failure at once, and `board_create_feature` /
  `board_update_feature` dry-run it when the card is written (`ready_check`,
  `board_check_ready`) — [`lifecycle.md`](lifecycle.md).
- **Publish gates + release freeze (cross-repo chains)** — a card can also wait on
  something OUTSIDE the board. `waits_for: npm:@protolabsai/ui@contains:protoLabsAI/protoContent@bd-a1`
  holds only once a published version is PROVEN (via its changesets git tag + GitHub
  compare) to contain card bd-a1's merge commit — `depends_on` releases at the MERGE, and
  a version floor can be met by an unrelated publish, both too early for a consumer that
  installs the package. Also `npm:<pkg>@<range>`, `release:<owner>/<repo>@[<package>@]<tag-or-range>`,
  `pr:<owner>/<repo>#<n>` (merged). The loop keeps the card out of the claim, visibly
  (`waiting on publish: …`), until every gate holds. And before auto-merging, the loop
  checks the PR's repo for a release in flight (`prepare-release*` branch/PR, an active
  `prepare-release.yml` run, or an untagged `chore: release v*` head; per-project
  `release_freeze`) and holds the merge until it lifts. [`publish-gates.md`](publish-gates.md) has the grammar and a worked
  design-system → npm → consumer example.
- **External QA panel (#473)** — when the repo's own review bot (protoreview's `QA panel`
  by default, `external_review`) FAILS a PR at its current head, the reconcile bounces the
  card into a fix round on the same PR with the panel's findings, once per head, and runs
  no merged-state gate and no merge while that FAIL stands. See
  [`lifecycle.md`](lifecycle.md#an-external-qa-panels-fail-at-the-head-473).
- **Escalation (opt-in)** — with a `coders` map of >1 distinct rung, a capability
  failure climbs to a stronger model. A rung may also hold SEVERAL interchangeable
  providers (`smart: [codex, sonnet]`, #362): the board round-robins across them, and on
  a rate limit it switches to the sibling immediately instead of backing off on the
  exhausted one. A provider that refuses its model outright (retired, not on the plan,
  client too old; #420) rotates the same way, and later cards start on a live sibling
  for 30 minutes. Climbing a rung means "a stronger model may succeed"; rotating within
  one means "this model is fine, its provider is not" — the two never mix.
- **coder.solve() board seam (ADR 0064 P2/P3)** — on a fresh build, when the
  [`coder`](https://github.com/protoLabsAI/protoAgent/tree/main/plugins/coder) plugin
  is enabled AND the feature has acceptance criteria AND `coder_solve_test_cmd` (or
  `local_gate_cmd`) is set, the loop dispatches through `coder.solve()`'s
  execution-grounded ladder — greedy → best-of-k → tree-search → **fusion** — instead
  of a single `delegate_to(acp)` shot, gated on the feature's acceptance tests
  actually PASSING in a real candidate worktree, never an LLM judge. **Fusion** (rung
  4, opt-in via `coder_solve_fusion_delegate`) is a richer *generator* for the
  hardest features the cheaper rungs couldn't pass — it can't tool-call (a plain
  completion, e.g. `protolabs/fusion`, not an ACP session), so `coder_seam.py` hands
  it the current content of the feature's declared files and writes its reply's
  files into a fresh worktree itself; the SAME `verify()` oracle judges it. Composes
  WITH the tier ladder above (solve() searches *within* a tier; a search that never
  passes escalates a tier, or blocks, exactly like a no-diff dispatch). Missing
  coder/acceptance/test command ⇒ honest degrade to the single shot; missing
  `coder_solve_fusion_delegate` ⇒ the ladder simply stops at tree-search — see
  `coder_seam.py`.
- **Rung diagnostic — `POST /api/plugins/project_board/features/{id}/test-rung`**
  (operator-only, no `@tool` wrapper): runs exactly ONE named rung
  (`greedy`/`best-of-k`/`tree-search`/`fusion`) against a feature's real acceptance
  tests, in a throwaway worktree that's ALWAYS reaped — never promoted, no PR, no
  board state touched. Verifying a specific rung — fusion especially, only
  otherwise reached after three cheaper rungs fail — shouldn't require contriving a
  task hard enough to fail its way there. `{"rung": "fusion"}` in the body; `coder`
  optional (defaults to `project_board.coder`).
- **Planning layer** — two reasoning subagents (`decompose` + `antagonist`) driven by
  the `decompose-project` skill: idea → outline → MADR ADRs → epics › milestones ›
  features, hardened by an adversary, with a per-epic human gate. Two more skills bracket
  it: **`onboard-project`** runs FIRST against a repo this board has not worked before —
  it scans for the preconditions a coding loop needs and auto-fixes the safe ones — and
  **`loop-retro`** runs after, mining the board's own attempt history into durable
  grounding so the next runs stop repeating known failures. **`cross-repo-chain`** is
  for work that spans repos through a release: the change in one repo, the publish, then
  the consumer card gated on it with `waits_for`. **`card-from-chat`** turns "add a feature
  for …" into an inline form, then a ready card the Board view follows (`board_watch`).
- **Console view** — a Kanban + list projection over the `/features` API (ADR 0026).
  `board_watch` opens it on the main stage and follows one card through its lanes.

It **composes** the upstream `delegates` plugin (ADR 0024/0025) for the ACP/A2A
spawn primitive — it does not reimplement it.


## A working example


Want a complete, working example of an agent built around this plugin?
**[roxy](https://github.com/protoLabsAI/roxy)** is a protoLabs operator/orchestrator
agent that installs this plugin as its coding-orchestration layer — it's the
reference host. It consumes this repo exactly the way you would (`plugin install` +
a pinned `plugins.lock`), enables it, and ships the surrounding agent (the A2A
server, the React console the **Board** view renders in, the delegate roster the
loop dispatches against, persona, evals). Read it to see how a board-driven coding
agent is wired end to end — including a live run shipping real features through the
board to a PR — or fork it as a starting point.
