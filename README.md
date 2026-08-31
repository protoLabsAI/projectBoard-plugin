# Project Board — coding orchestration plugin

A **protoAgent plugin** that turns an idea into merged PRs: a lean 6-state board
backed by [beads-rust](https://github.com/Dicklesworthstone/beads_rust) (`br`), an **ACP spawn loop**
that dispatches a coding agent per feature into an isolated git worktree, an
adversarial **planning layer**, and a Kanban/list **console view**.

Install into any protoAgent agent from this git URL — it's not tied to any one agent.

```
backlog → ready → in_progress → in_review → done
                      │
                      └── blocked  (a flag, not a lane)
```

## See it running — a working board-driven agent

Want a complete, working example of an agent built around this plugin?
**[roxy](https://github.com/protoLabsAI/roxy)** is a protoLabs operator/orchestrator
agent that installs this plugin as its coding-orchestration layer — it's the
reference host. It consumes this repo exactly the way you would (`plugin install` +
a pinned `plugins.lock`), enables it, and ships the surrounding agent (the A2A
server, the React console the **Board** view renders in, the delegate roster the
loop dispatches against, persona, evals). Read it to see how a board-driven coding
agent is wired end to end — including a live run shipping real features through the
board to a PR — or fork it as a starting point.

## What it does

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
  already-opened PR → `in_review`, else reset → `ready`).
- **DAG + gates** — `depends_on` are `blocks` edges; a dependent stays out of the
  puller until its blocker is **merged** (foundation merge-gate). The **Ready gate**
  requires a spec, EARS acceptance criteria, and explicit `files_to_modify`.
- **Escalation (opt-in)** — with a `coders` map of >1 distinct delegate, a capability
  failure climbs to a stronger model. A rung may also hold SEVERAL interchangeable
  providers (`smart: [codex, sonnet]`, #362): the board round-robins across them, and on
  a rate limit it switches to the sibling immediately instead of backing off on the
  exhausted one. Climbing a rung means "a stronger model may succeed"; rotating within one
  means "this model is fine, its quota is not" — the two never mix.
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
  features, hardened by an adversary, with a per-epic human gate.
- **Console view** — a Kanban + list projection over the `/features` API (ADR 0026).

It **composes** the upstream `delegates` plugin (ADR 0024/0025) for the ACP/A2A
spawn primitive — it does not reimplement it.

## Requirements

- **protoAgent ≥ 0.153.2** (tabbed plugin Configure dialogs and sandboxed custom
  Configure views; protoAgent #3179/#3180).
- **beads-rust** — the **`br`** CLI, the board's DAG/status store. **Fetched for you on
  first run (v0.43.0)**: with no `br` on `PATH` the plugin downloads the pinned release
  (`br_fetch.BR_VERSION`, sha256-verified per platform) into the instance's plugin-data
  dir and uses it — see "br fetched on first run" below. To install by hand:
  `cargo install beads_rust`. NOT the stale homebrew `bd` (a different, write-broken
  package); the `bd-`/`br-` prefix in issue ids is just the workspace namespace.
  Override the binary with `BR_BIN` (it always wins over a fetched one).
- `git` + the **`gh`** CLI (authenticated) for branch push + PR creation.
- The **`delegates`** plugin enabled, with an **`acp`** coder delegate declared.
  **[`proto`](https://github.com/protoLabsAI/protoCLI) is the first-class coder** —
  it's the purpose-built protoLabs coding agent, speaks ACP natively (`proto --acp`),
  and runs its full long-horizon harness (durable session-memory checkpoint,
  compaction, memory consolidation) over ACP, so it holds context across a long
  feature build. Any ACP agent works (Claude Code, Codex, Gemini CLI), but **proto is
  the recommended choice** — *recommended*, not defaulted: `project_board.coder` has
  **no default** and must name the delegate you declared. A reviewer `a2a` delegate is
  optional (review dispatch is off by default — most fleets review PRs via a pipeline
  on open).

All four externals — `br`, `gh`, the coder delegate, the bound repo — are checked by
the **setup preflight** (below) at register time and every loop tick, so a host that
is missing one says so instead of booting green.

## Install

```bash
python -m server plugin install https://github.com/protoLabsAI/projectBoard-plugin --ref main
python -m server plugin enable project_board          # the trust decision; then restart
```

Then in `config/langgraph-config.yaml`:

```yaml
plugins:
  enabled: [delegates, project_board]

delegates:
  - { name: proto, type: acp, command: proto, args: ["--acp"], workdir: ~/dev/my-repo, permissions: allowlist }

project_board:
  coder: proto               # REQUIRED — the acp delegate the loop dispatches to (protoCLI
                             # here). There is NO default (v0.42.0): unset, the setup
                             # preflight below flags it and the loop pauses instead of
                             # dispatching to a phantom name. LIVE: it is a console
                             # Settings field — naming it there resumes a paused loop
                             # on its next check, no restart. Leave it blank ONLY with a
                             # `coders:` ladder that maps every tier (smart/reasoning/opus).
  repo: ~/dev/my-repo
  base_branch: main
  # db_path: /somewhere/board/beads.db
                             # LEAVE UNSET (the default): the board keeps ONE beads store
                             # per instance — <instance plugin-data>/project_board/.beads/
                             # beads.db, bootstrapped automatically on first use. Every
                             # project shares it, and `br init` never runs inside your
                             # project repos. Set it only to pin the board db to an
                             # explicit file of your own — see "Where the board lives".
  loop_enabled: false        # flip true to start the background puller
  max_concurrent: 1          # >1 builds features in parallel (each its own worktree).
                             # FEATURE-level: one drive per slot. Within each drive the
                             # best-of-k rung dispatches coder_solve_k ACP sessions
                             # concurrently, so peak ACP processes =
                             # max_concurrent × coder_solve_k (default: 1 × 3 = 3).
                             # Use max_concurrent_sessions to cap the within-drive parallelism.
                             # LIVE: coder, br_autofetch, max_concurrent, max_pending_reviews
                             # and max_concurrent_sessions are console Settings fields
                             # (Settings → Plugins → Project Board) and a save applies
                             # them to the RUNNING loop on its next tick — no restart.
                             # Every other key here is read once at boot. On a
                             # multi-project board size max_concurrent to the project
                             # count (one slot per repo) or one deep queue starves the rest.
  merge_poll: true           # poll merged PRs as a fallback to the webhook Done edge
  auto_merge: false          # OPT-IN, LIVE (console field). The MERGE edge: once an in_review
                             # PR is green by every gate the loop runs — GitHub CLEAN (required
                             # checks + branch protection), merged-state verdict stamped against
                             # the CURRENT base, review gate `review-clean` — merge it; the board
                             # flips to done via the normal Done edge. Off = park green PRs for a
                             # human/agent adjudicator (which is only as durable as whatever
                             # schedules it) — those cards then carry
                             # next_action = "awaiting-merge (auto_merge off)" in board_list,
                             # /features and the console chip (#208), so the PM leads its
                             # status report with "merge #N or turn auto_merge on" instead
                             # of re-offering a review. Label a card `merge-hold` to exempt it.
  merge_method: squash       # squash | merge | rebase
  merged_verify_max: 5       # sibling merges a held in_review card can survive (one gate run each,
                             # only when base moved) before its merged-state verdict stops being
                             # refreshed. 0 = unlimited. Exhaustion holds the auto-merge edge.
  goal_verify: false         # flip true: verify the coder's diff vs acceptance_criteria before opening a PR
  max_mode_n: 1              # >1 = best-of-N "Max-Mode": N coders per feature, keep the best diff
  local_gate_cmd: "auto"     # pre-PR gate (the FAST slice of CI — lint/typecheck/unit,
                             # NOT the full suite), run in each worktree before a PR opens.
                             # "auto" = DISCOVER it from the bound repo, ecosystem-neutral:
                             # a package.json gate/ci/check/verify script → `pnpm run <it>`;
                             # a Makefile/justfile gate/ci/check target → `make/just <it>`
                             # (Python/Rust/Go); else the `pnpm -r --if-present typecheck
                             # build test` superset. `gate` wins first so a repo can point
                             # coders at a fast slice distinct from a heavy `ci`. Prefer a
                             # repo-DECLARED target whose OWN CI calls the same thing, so
                             # local == CI and can't drift. Explicit command overrides; blank
                             # = no gate. NOTE: `auto` resolves at construction — the repo
                             # must be cloned before the loop starts. See "The gate" below.
  preflight: true            # fail-CLOSED smoke of local_gate_cmd on the clean base before
                             # dispatching ANY work: an UNRUNNABLE gate (missing tool, base
                             # broken) HOLDS all ready work (visible on the board) instead of
                             # burning generations no coder could pass. Re-checks each cycle,
                             # releases on recovery. A slow gate times out → indeterminate →
                             # allow (never wedge the board). Set false to skip.
  # With local_gate_cmd set, Max-Mode is EXECUTION-GROUNDED (ADR 0064): the winner is
  # picked from candidates whose gate actually PASSES; the LLM judge only breaks ties
  # among the passing set (or decides when no gate is set / none pass).
  coder_solve: true          # OPT-OUT valve for the ADR 0064 P2 seam (default on; the
                             # real gate below still requires the `coder` plugin +
                             # acceptance criteria + a test command — see "What it does").
  coder_solve_test_cmd: "pytest tests/ -q"  # solve()'s verify() oracle; falls back to
                             # local_gate_cmd if blank, else the seam honest-degrades.
  coder_solve_fusion_delegate: ""  # rung 4 (ADR 0064 P3), opt-in: an `openai`-type
                             # delegate name (e.g. protolabs/fusion) for the hardest
                             # features. Blank (default) = ladder stops at tree-search.
  coder_solve_fusion_k: 2    # candidates fusion generates when reached
  max_concurrent_sessions: 0 # cap concurrent ACP processes within a single drive's solve.
                             # 0 (default) = unlimited within the k budget (best-of-k
                             # candidates run in parallel). Set to 1 to run k candidates
                             # sequentially — useful when the host supports only one ACP
                             # process at a time. Peak without this cap:
                             # max_concurrent × coder_solve_k.
  # webhook_secret: "..."    # required HMAC for public merge/CI/review ingress
```

**Where the board lives.** With no `db_path` configured (the shipped default — a
blank or absent key are the same thing) the board keeps **one beads store per
instance**: `<instance plugin-data>/project_board/.beads/beads.db`, bootstrapped
automatically (`br init`, cwd'd in the store root) the first time the board is
touched. Every project on a multi-repo board shares that one store — one board,
one work graph, one id namespace — and the plugin **never runs `br init` inside a
project repo**, so onboarding a second (or tenth) repo can't fragment the board
across per-repo `.beads/` workspaces. Setting `db_path` to an explicit file is the
documented operator override: the path is passed **verbatim** (`--db`) to every
board op — the loop, the HTTP API, and the board tools all pin to it — and nothing
is created on your behalf (`br init` it yourself). The pre-D3 behavior where a
blank `db_path` meant per-repo `.beads/` auto-discovery is gone; on a
multi-project board an explicitly blank `db_path` is additionally surfaced by the
setup preflight as a non-blocking "stale override" advisory.

**Upgrading a pre-D3 board.** Before this default existed, a board with no
`db_path` kept its cards *inside the configured repo* (`br` per-repo discovery,
`<repo>/.beads/*.db`). Those workspaces are not read anymore — but the switch is
never silent: the setup preflight detects a configured repo that still carries a
`.beads/` workspace while no `db_path` is set and raises a non-blocking migration
advisory (an operator warning and a board-page callout naming the repo). To keep
reading the old cards, set `db_path` to that `<repo>/.beads/<file>.db` — the
explicit pin **is** the old store, unchanged. To adopt the instance store instead,
move the cards over yourself (the old workspace stays untouched); the advisory
quiets once `db_path` is pinned explicitly or the repo no longer carries a
`.beads/` workspace.

## Use

- **Headless / via the agent:** `board_create_epic`, `board_create_feature`
  (`title`, `spec`, `acceptance_criteria`, `files_to_modify`, `depends_on`, …),
  `board_mark_ready`, `board_list`. Every `in_review` row of `board_list` (and of
  `GET …/features`) carries `next_action` — `awaiting-merge (auto_merge off)` /
  `auto-merge pending` / `review in progress` / `changes requested` / `awaiting
  review verdict (no review-clean)` / `merge-hold (operator veto)` / `blocked` /
  `draft (run gh pr ready)` — plus `awaiting_merge: true` and a `next_action_hint`
  ("auto_merge is off — merge #N or turn it on in Settings ▸ Project Board") for the
  first. Derived from the review sub-state labels + the board's LIVE
  `auto_merge`/`review_gate` config (the same decoding the loop's merge edge uses;
  `store.merge_posture`; a Settings save to `auto_merge` flips it with no restart),
  no network. `board_list(with_ci=true)` demotes a red row to `ci failing` — never
  "merge #N" on a red PR.
- **Plan a project:** the `decompose-project` skill ("decompose <idea>") runs the
  adversarial pipeline and populates the board.
- **HTTP API:** operator reads and mutations live under the bearer-gated
  `/api/plugins/project_board/*` prefix. The public prefix exposes only the board
  iframe plus `/webhook/pr`, `/features/{id}/ci`, and `/features/{id}/review` for
  external systems. Every public POST requires
  `X-Hub-Signature-256: sha256=<HMAC-SHA256(raw-body, webhook_secret)>`; a blank
  secret disables public mutations with 503. GitHub signs `/webhook/pr` natively;
  CI/review callers must sign the exact JSON bytes they send.
- **Watch it:** the **Board** console view (left-rail) at
  `/plugins/project_board/board` — Kanban + list, live-refreshing, served by the
  same router as the API (so the declared view path is genuinely mounted).

## The task lane — deliverables, not PRs

Not every board card ships code. A **task-type bead** (`issue_type: task`, #217) rides
the SAME rails as a coding feature — `ready → in_progress → in_review → done` — but its
output is a **deliverable** (a doc, a decision, an artifact ref), not a PR.
`record_delivery` (`board_deliver`) moves it to `in_review` with no `pr_url`, stamping
the deliverable text plus a `delivered-by: <actor>` note — the assignee **at delivery
time**, captured then so a later reassignment can't rewrite who actually delivered it.

Because a task has no PR to merge, its **Done edge is a verifier's approval**, not
`record_merge`: `board_verify` (the agent tool) / `POST …/features/{id}/verify` (the
HTTP API) / `record_verification` (the store) close an approved task with an auditable
`verified: <who>` reason — a deliberate *second* `br close` edge beside the code path's
one Done edge. A rejection instead records the feedback as a comment (the re-dispatch
prompt leads with it) and requeues the bead to `ready` for another pass.

### Self-verification is recorded, not refused

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

### The default verifier splits by caller — on purpose

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

## Configure it in the console

On protoAgent 0.153.2+, **Settings → Plugins → Project Board → Configure** is split
by operator intent:

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

## The gate — the coder's fast slice of CI

The **pre-PR gate** (`local_gate_cmd`) is the command the loop runs in each coder's
worktree before opening a PR, so the coder's own solve-loop iterates to **green**
locally instead of shipping a PR that only fails in CI.

### Two tiers — the gate is NOT a full-CI replica

| | Local gate (this) | CI |
|---|---|---|
| **Question** | "is my code correct?" | "is it releasable?" |
| **Runs** | every worktree, every attempt | once per PR |
| **Contains** | lint + typecheck + **unit** tests — fast, hermetic, deterministic | everything: integration, cross-platform matrix, image build, release, deploy |
| **Owner** | the coder's iterate loop | the human merge + the loop's CI-bounce re-dispatch |

You **never replicate a complex CI locally**. Anything needing services, secrets, a
matrix, network, or an image build stays CI-only — the PR still runs it, and whatever
the local slice didn't catch comes back to the coder via the CI-bounce. The gate's job
is to kill the cheap, common failures in seconds so the loop isn't a slow CI-bounce
casino. Getting that slice faithful matters — the failure modes are subtle (a
build-only gate compiles a test file but never runs it; a build+test gate still misses
`typecheck`, since most test runners strip types without checking them).

### `auto` — discover it, don't transcribe it

A hand-copied gate rots the moment the repo's CI changes, and is wrong the instant a
team is pointed at another repo. So:

```yaml
project_board:
  local_gate_cmd: "auto"
```

`auto` **discovers** the gate from the bound repo — **ecosystem-neutral**, keyed on how
the repo builds, always preferring a single repo-**declared** target:

1. `package.json` script `gate` / `ci` / `check` / `verify` → `pnpm run <it>` *(node)*
2. `Makefile` / `justfile` `gate` / `ci` / `check` target → `make <it>` / `just <it>`
   *(Python / Rust / Go / anything — e.g. `make gate` = `ruff check . && pytest -q`)*
3. `package.json`, none declared → `pnpm -r --if-present typecheck build test`
4. nothing recognized → gateless (fail-open, warns)

`gate` is checked **first**: it's the unambiguous "this is the fast coder slice", so a
repo whose `ci` target is the whole heavy suite points coders at `gate` and the loop
won't grab the heavy one. An explicit command overrides; blank still = no gate.

### Make your repo team-ready

Give the team **one gate target** — the fast slice — and have your own CI call the
**same** target, so local == CI by construction. Node:

```jsonc
// package.json                                   ci.yml:  - run: pnpm run gate
"scripts": { "gate": "pnpm -r typecheck && pnpm -r --if-present test" }
```
> Invoke it `pnpm run gate` — `pnpm ci`/`pnpm gate` shorthands can collide with pnpm builtins.

Python (protoAgent-shaped: a 9-workflow CI, but only `checks.yml` — ruff + pytest — is
the coder's concern; the matrix / docker-publish / release / deploy workflows are
`push`/`tag`/`dispatch` triggered and never a pre-PR gate):

```makefile
# Makefile                                        checks.yml:  - run: make gate
gate:                       ## the coder's fast slice — lint + unit tests, no services
	ruff check .
	pytest tests/ -q -m "not integration"
```

Same shape in a `justfile` (`just gate`), `nox` (`make gate` → `nox -s gate`), Cargo
(`make gate` → `cargo clippy && cargo test`), etc. The heavy jobs stay in their own
workflows; the coder never runs them.

### Preflight (fail-closed)

Before dispatching **any** work, the loop smoke-runs the resolved gate on the clean
base checkout (`preflight: true`, the default). If the gate can't even launch
(missing tool, broken deps, base already red) it **holds** all ready work — flagged
blocked, with the reason, visible on the board — rather than burn generations no coder
could pass, and re-checks each cycle so work resumes the moment it's fixed. A slow gate
that times out is treated as indeterminate → allowed (a slow gate must never wedge the
board). This is the fail-**closed** complement to the per-PR gate's fail-**open**: a
flaky gate never blocks good work, but an *unrunnable* gate never starts bad work.

### Environment — what gate, format, and verify children see

Every subprocess the loop spawns to run repo-defined commands over coder-written
code — the gate preflight, the pre-PR `local_gate_cmd`, the auto-fix `format_cmd`,
and the `coder.solve()` acceptance-test (verify) run — receives a **narrow
allowlist** environment, not the host's. The child sees only the baseline a
build/test toolchain needs — `PATH`, `HOME`, `LANG`/`LC_*`, `TMPDIR`, `TERM`,
`SHELL`, `USER`, `CI`, plus the Windows system mirror of the same (`SYSTEMROOT`
above all) — and nothing else. In particular the host agent's
identity/credential block (`AGENT_NAME`, `PROTOAGENT_*`, `A2A_*`) never reaches
these children.

A deployment whose gate or tests genuinely need another variable names it in the
`env_passthrough` list — the single escape hatch through the allowlist (a list or
a comma-separated string):

```yaml
project_board:
  env_passthrough: [DATABASE_URL, NODE_OPTIONS]
```

The coder's own ACP session environment is host-managed and stays on the looser
**blacklist** tier — it keeps everything except the host identity/credential
block, with the same `env_passthrough` override; tightening that path is tracked
separately.

### Setup preflight — can the board run at all? (v0.42.0)

Distinct from the gate preflight above (which asks "can the *repo's tests* run"), the
setup preflight asks "can the *board* run": four checks, computed by
`setup_check.setup_status(cfg)` — pure, never raising, never a `br` board op (one
cached `br --version` at most):

| key | ok when | hint (operator copy) |
|---|---|---|
| `br` | the beads CLI resolves (`BR_BIN` > the auto-fetched binary > `br` on PATH) | "fetching beads-rust vX for <platform> …" while the auto-fetch runs; the download error + the install hint if it failed; the install hint if `br_autofetch` is off / `BR_BIN` is set but unresolvable / the platform has no build (Windows, musl) |
| `gh` | the GitHub CLI is on PATH | install it + `gh auth login`; builds can't open PRs until then |
| `coder` | **every** configured coder name (`coder`, the `coders` tier map, each `projects:` entry's `coders`) resolves to a live `acp` delegate — **no names configured is a failure**. With `coder` **blank**, the ladder is the only dispatch path: escalation must be on (>1 distinct delegate) and the instance map *and* every project map must cover **every** tier (`smart`/`reasoning`/`opus`) — an unmapped rung dispatches to `''` and blocks the card | "no coder configured — pick a delegate in Settings ▸ Project Board or let the agent propose_delegate (the former implicit default `proto` no longer applies — set `coder: proto` to keep it)" / names the unresolvable delegate / names the uncovered tier(s) |
| `repo` | the board is bound (explicit `repo`/`db_path`/`projects:`) to a directory that exists — or the shipped `repo: "."` default and the cwd already has a `.beads/` | set `project_board.repo` to the checkout's absolute path |

Where it surfaces:

- **`GET /api/plugins/project_board/status`** → `setup: {br, gh, coder, repo, loop_enabled,
  loop_blockers, loop_cfg_stale, loop_cfg_stale_keys, loop_cfg_stale_hint, ready}` alongside
  the v0.40.0 `bound` keys. `loop_cfg_stale` is the reload-drift tell: a config reload
  rebuilds the routers on the NEW config while the running loop keeps its construction-time
  `coders` / `repo` / `base_branch` / `db_path` / `projects` — the status compares the two
  and says **"restart the agent to apply"** (on its own line, on the affected hint, and as
  the `loop` host warning) instead of reporting the new config as the loop's state.
  `coder` is live (applied by `reload()`), so it never goes stale.
- **The board page** renders each failing check with its hint (a warning card above the
  board, or in place of the raw error when the board can't be read at all).
- **Host operator warnings** — each failing check is forwarded to the host's
  `registry.report_setup_gap(key, message)` seam (keys `br`/`gh`/`coder`/`repo`, plus
  `loop` for the stale-config note; `None` clears it on recovery), which the console shows
  in `GET /api/runtime/status`. Edge-triggered after a first evaluation that sends every
  key unconditionally — so a reload's fresh reporter clears a warning the previous
  instance raised. Guarded: a host without the seam just gets the log lines.
- **The loop pauses, it doesn't traceback.** With `loop_enabled: true` and a blocker
  standing (`br`, `coder`, `repo` — a missing `gh` only fails the PR edge, so it is
  reported but not paused on) the puller logs ONE `loop paused: …` warning and
  re-checks every `loop_interval_s` (off the event loop) — install `br`, declare the
  delegate, name it in the `coder` Settings field, bind the repo, and it runs crash
  recovery + starts ticking on its own. No restart. Before v0.42.0 the
  same board booted green and logged `crash recovery failed` + `loop tick failed`
  tracebacks every tick.

### br fetched on first run (v0.43.0)

A fresh member should not need a Rust toolchain to get its board store. When the setup
preflight finds no `br` — and `project_board.br_autofetch` is on (the default; a live
console Settings field) — the plugin:

1. picks the **pinned** beads-rust release for this platform (`br_fetch.BR_VERSION`;
   `darwin_arm64`, `darwin_amd64`, `linux_amd64`, `linux_arm64` — **not Windows** and
   **not musl/Alpine** (the assets are glibc builds), both of which get a clear install
   hint),
2. downloads `br-<version>-<platform>.tar.gz` from the beads_rust GitHub releases page
   **off the event loop**, once per process, bounded to 60 s,
3. verifies its **sha256** against the table in `br_fetch.py` (the release's own
   per-asset checksums — the same pin-and-checksum discipline as `.github/workflows/ci.yml`,
   which runs the real-br shape tier on exactly this version; a test pins the two together),
4. extracts only the `br` binary to `<instance plugin-data>/project_board/bin/<version>/br`
   (the host's `instance_paths().store("plugin-data")` — writable on desktop, never the
   plugin's own source checkout; override with `PROJECT_BOARD_DATA_DIR`), mode 0755,
   atomically. The path is keyed by version, so a pin bump fetches the new release
   instead of keeping a stale binary; delete `<data>/project_board/bin` to force a
   re-fetch on the next restart,
5. re-points the store at it **in place** — the paused loop resumes on its next check,
   `/status` reports `br.source: "fetched"`, the board page says "br vX fetched to …".

Resolution order for the binary the store shells: **`BR_BIN` env > fetched binary > `br`
on PATH** — an explicit `BR_BIN` is never overridden, and a `BR_BIN` that does not
resolve is never "fixed" by a fetch (the hint names it). A failed fetch (offline, a
checksum mismatch, an egress block) is a `br` setup gap with the error in the hint and
the manual install as the fallback — never a traceback; the fetch runs once per process,
so a restart retries it. Set `br_autofetch: false` for the pre-0.43 posture (a missing
`br` is just the install hint); flipping it off while a download is in flight neither
aborts nor forgets it, and flipping it back on never starts a second one.

**Egress:** the download is one HTTPS GET to `github.com`, which 302s to
`release-assets.githubusercontent.com`. A deployment with the host's egress allowlist
(ADR 0008) must allow both hosts; the fetch consults the allowlist on the initial URL
**and on every redirect hop**, and refuses a hop that leaves `*.githubusercontent.com`
— reporting the allowlist's message instead of a socket error.

## Reference

| Doc | What |
|---|---|
| [`docs/api.md`](docs/api.md) | every HTTP route — the operator (bearer) and public (HMAC) surfaces |
| [`docs/tools.md`](docs/tools.md) | every agent tool, with the lifecycle-changing ones flagged |
| [`docs/configuration.md`](docs/configuration.md) | every config key, its default, and whether the change is live / reload / restart |
| [`docs/adr/`](docs/adr/) | the decision records for subsystem behaviour |

Both references are covered by `tests/test_docs_reference.py`, which fails if a route or
tool is added, renamed or removed without the doc changing — so they are worth trusting.

## External-seam coverage — REAL, EXEMPT, or honest debt

A function whose whole job is to make an external system do something is close to untested
when it is validated only against a mock of that system — that shape shipped three GREEN-
but-inert fixes in one night (#353, #354, #356). `tests/test_external_seams.py` makes that
gap impossible to add silently: every seam that shells `gh`/`git`/`br` is CLASSIFIED, by
AST rather than by name, as **REAL** (exercised against the real binary/API in the
integration tier), **EXEMPT: `<reason>`** (real coverage genuinely not warranted, reason
stated), or **UNCOVERED** (honest debt) — and the UNCOVERED count is a ratchet that may
fall, never rise.

`worktree.py` reached its final contract over #361: **23 REAL / 3 EXEMPT / 0 UNCOVERED**
(`MAX_UNCOVERED_WORKTREE = 0`). The 11 local-git seams run against a real bare-origin +
clone (slice 1); the 12 read-dominant `gh` seams run against a pinned, permanently-open PR
with `PB_REQUIRE_GH=1` so an absent credential FAILS rather than skips (slice 2).

The remaining three are the **PR-lifecycle writes** — `open_pr`, `close_pr`,
`_promote_adopted_draft` — classified **EXEMPT** (slice 3). Each mutates real PR lifecycle
state (create a PR, close a PR, promote an adopted draft to ready), so a REAL tier could
only run by creating and tearing down real PRs against a **disposable sandbox repository**,
and the operator's decision is not to provision that sandbox now. This is a deliberately
recorded gap, **not mock confidence** and **not an untested path**: these three run
repeatedly in normal board operation — every delivery opens a PR, closes stale ones and
promotes adopted drafts — so they ARE exercised in production. The residual weakness is the
**feedback channel**: a regression surfaces as a blocked card / operator signal rather than
red CI. They are never mocked into a false REAL, and no CI job creates, closes or promotes a
PR in a production repository. If the sandbox is ever provisioned, these flip to REAL and the
exemption disappears.

Because an EXEMPT drops a seam from the ratchet, the exemption is **not taken on faith** —
it is verified against the source. `test_external_seams.py` asserts by AST that each of the
three EXEMPT seams really shells a *fixture-destroying* `gh pr` write (`create` / `close` /
`ready`) — the mutations that would consume the pinned read fixture — and, symmetrically,
that **no** REAL seam shells one. So a read cannot hide under EXEMPT to escape the ratchet,
and a mock cannot be relabeled REAL while mutating a real PR: either would fail the suite
loudly rather than leave the contract green. `merge_pr` stays REAL because branch protection
*refuses* `gh pr merge` on the pinned PR, so it runs for real without consuming it.

EXEMPT is also **not an escape *from* the ratchet** — it is a second ratchet.
`MAX_EXEMPT_WORKTREE` may only *fall* (each write flips to REAL the day a disposable sandbox
repo is provisioned), never rise, so a fourth EXEMPT can't be minted to make debt disappear.
And the exemption is backed by an **executable escape hatch**:
`tests/test_worktree_pr_lifecycle_sandbox.py` drives all three writes — create, promote-draft,
close — against a *throwaway* sandbox repo through the real `gh`, dormant (skipped) only until
the operator sets `PB_SANDBOX_REPO` (and `PB_REQUIRE_SANDBOX=1` to enforce it); it refuses to
run against the checkout's own repo, so no CI job ever mutates a production PR. That test's
presence and its coverage of each EXEMPT seam are themselves asserted by
`test_external_seams.py` — so the recorded gap is one env var from closing, not a promise on
paper, and a regression that stops a seam issuing its `gh pr` write fails the contract today.

## Layout

| File | What |
|---|---|
| `store.py` | the `br`/beads wrapper — board projection + the Ready/Done invariants |
| `loop/` | the puller, split by execution edge (#268): `core.py` (lifecycle + config), `drive.py` (`ready → worktree → coder → PR`), `reconcile.py` (rebase/CI/verify/merge), `preflight.py`, `prompt.py` |
| `worktree.py` | per-feature worktree lifecycle, scoped coder dispatch, `open_pr` |
| `coder_seam.py` | the ADR 0064 P2 seam — dispatches a build through `coder.solve()` when available, else honest-degrades |
| `api.py` | the HTTP API + the `/webhook/pr` Done edge (HMAC-verified) |
| `setup_check.py` | the setup preflight (`br`/`gh`/coder/repo) + the host gap reporter — can the board run at all? |
| `br_fetch.py` | `br` fetched on first run: the pinned beads-rust release + sha256 table, the off-loop once-per-process fetch, `BR_BIN` > fetched > PATH resolution |
| `board_view.py` | the Kanban/list console view |
| `retro.py` | loop-retro mining: bead attempt/outcome history → recurring failure classes (the self-improving flywheel) |
| `subagents.py` + `skills/` | the `decompose`/`antagonist` planning layer + the `loop-retro` distill skill |
| `__init__.py` | `register()` — wires it all |

Ships **disabled**; nothing runs until you enable it, declare a coder delegate and name
it in `coder:`.

## Standalone scripts (outside pytest)

`from project_board import coder_seam` resolves under `pytest` because
`tests/conftest.py` registers this repo's root `__init__.py` under the name
`project_board` directly in `sys.modules` (`importlib.util.spec_from_file_location`,
`submodule_search_locations=[ROOT]`) — the repo's own directory name
(`projectBoard-plugin`) doesn't matter; no symlink, no rename needed. That
registration only happens when `conftest.py` loads, so a plain script
(`python some_smoke_test.py`, not `pytest`) needs the same few lines up front:

```python
import importlib.util
import sys
from pathlib import Path

ROOT = Path("/path/to/projectBoard-plugin")
spec = importlib.util.spec_from_file_location("project_board", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
sys.modules["project_board"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sys.modules["project_board"])

from project_board import coder_seam, worktree  # now resolves
```

Handy for a one-off live smoke test (e.g. exercising `coder_seam.test_rung()`
against a real repo + a real delegate) without standing up a whole plugin host.

## Releasing

Releases follow the fleet cadence via [`protoLabsAI/release-tools`](https://github.com/protoLabsAI/release-tools):
**tag → LLM-themed release notes → Discord embed → GitHub release body**, wired in
`.github/workflows/release.yml`.

The version lives in `protoagent.plugin.yaml` + `pyproject.toml` (kept in lockstep by a
test) and is bumped per feature PR. To **cut a release** that batches the bumped changes
since the last tag, either:

- push a `chore: release vX.Y.Z` commit to `main`, or
- run the **Release** workflow manually — `gh workflow run release.yml` (or the Actions tab).

It tags the current version, generates notes for the range since the previous tag, posts
them to the release Discord channel, and sets the GitHub release body — idempotent
(a re-run on an already-tagged version is a no-op). Requires the org secrets
`GATEWAY_API_KEY` + `DISCORD_RELEASE_WEBHOOK`.
