# Annotated configuration

A commented `project_board:` block with the common keys, then where the board lives. The complete key list, with defaults and live/reload/restart behavior, is in [configuration.md](configuration.md).

Then in `config/langgraph-config.yaml`:

```yaml
plugins:
  enabled: [delegates, project_board]

delegates:
  - { name: proto, type: acp, command: proto, args: ["--acp"], workdir: ~/dev/my-repo, permissions: allowlist }

project_board:
  coder: proto               # REQUIRED — the acp delegate the loop dispatches to (protoCLI
                             # here). There is NO default (v0.42.0): unset, the setup
                             # preflight (setup-preflight.md) flags it and the loop pauses instead of
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
  require_complete_review: false
                             # OPT-IN, per project (set it in a `projects:` entry, or here as
                             # the fallback). On: the auto-merge edge will NOT merge while the
                             # external QA panel's pass at the PR head is INCOMPLETE — the
                             # `QA panel` check concluded `neutral` ("Incomplete pass — not
                             # blocking", `hold:incomplete-coverage`) or the panel's review
                             # marker says `complete=false`. GitHub reads `neutral` as passing,
                             # so without this an incomplete pass merges. The card stays
                             # in_review reading "awaiting complete review (panel pass was
                             # incomplete)", and the board comments `@vera review` on the PR
                             # ONCE per head to ask for a full pass. A complete pass later →
                             # the normal merge. Needs `external_review` on (the default).
  review_summon_handle: vera # who that re-review comment mentions (`@<handle> review`).
                             # Blank = hold without posting. Per project, like the above.
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
                             # must be cloned before the loop starts. See gate.md.
  preflight: true            # fail-CLOSED smoke of local_gate_cmd on the clean base before
                             # dispatching ANY work: an UNRUNNABLE gate (missing tool, base
                             # broken) HOLDS all ready work (visible on the board) instead of
                             # burning generations no coder could pass. Re-checks each cycle,
                             # releases on recovery. A slow gate times out → indeterminate →
                             # allow (never wedge the board). Set false to skip.
  preflight_cmd: ""          # a cheaper check to smoke instead of the gate (lint + an
                             # import check). Blank = smoke local_gate_cmd (#456).
  # With local_gate_cmd set, Max-Mode is EXECUTION-GROUNDED (ADR 0064): the winner is
  # picked from candidates whose gate actually PASSES; the LLM judge only breaks ties
  # among the passing set (or decides when no gate is set / none pass).
  coder_solve: true          # OPT-OUT valve for the ADR 0064 P2 seam (default on; the
                             # real gate still requires the `coder` plugin +
                             # acceptance criteria + a test command — see overview.md).
  coder_solve_test_cmd: "pytest tests/ -q"  # solve()'s verify() oracle; falls back to
                             # local_gate_cmd if blank, else the seam honest-degrades.
  # coder_solve_test_paths:  # per-path oracles (#459): first matching glob wins per
  #   "apps/web/**": "npm ci && npx tsc --noEmit && npx vitest run"   # changed file;
  #   "**": gate             # `gate` = local_gate_cmd. See docs/configuration.md.
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
