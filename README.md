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
  separate store, so the work graph can't drift out of sync.
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
  failure climbs a model tier (`fast→smart→reasoning`) and blocks at the top.
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

- **protoAgent ≥ 0.27.0** (console views + the ACP delegate teardown).
- **beads-rust** — the **`br`** CLI on `PATH`, the board's DAG/status store. Install
  with `cargo install beads_rust`. NOT the stale homebrew `bd` (a different, write-
  broken package); the `bd-`/`br-` prefix in issue ids is just the workspace
  namespace. Override the binary with `BR_BIN` if needed.
- `git` + the **`gh`** CLI (authenticated) for branch push + PR creation.
- The **`delegates`** plugin enabled, with an **`acp`** coder delegate declared.
  **[`proto`](https://github.com/protoLabsAI/protoCLI) is the first-class coder** —
  it's the purpose-built protoLabs coding agent, speaks ACP natively (`proto --acp`),
  and runs its full long-horizon harness (durable session-memory checkpoint,
  compaction, memory consolidation) over ACP, so it holds context across a long
  feature build. Any ACP agent works (Claude Code, Codex, Gemini CLI), but **proto is
  the recommended default**. A reviewer `a2a` delegate is optional (review dispatch is
  off by default — most fleets review PRs via a pipeline on open).

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
  coder: proto               # the first-class ACP coder (protoCLI)
  repo: ~/dev/my-repo
  base_branch: main
  loop_enabled: false        # flip true to start the background puller
  max_concurrent: 1          # >1 builds features in parallel (each its own worktree)
  merge_poll: true           # poll merged PRs as a fallback to the webhook Done edge
  goal_verify: false         # flip true: verify the coder's diff vs acceptance_criteria before opening a PR
  max_mode_n: 1              # >1 = best-of-N "Max-Mode": N coders per feature, keep the best diff
  local_gate_cmd: "auto"     # pre-PR gate, run in each worktree before a PR opens. "auto"
                             # = DISCOVER it from the bound repo (a package.json ci/check/
                             # verify script → `pnpm run <it>`; a Makefile/justfile ci
                             # target → `make/just <it>`; else the `pnpm -r --if-present
                             # typecheck build test` superset). Prefer a repo-DECLARED
                             # entrypoint whose OWN CI calls the same target, so local == CI
                             # and can't drift. Give an explicit command to override; blank
                             # = no gate. NOTE: `auto` resolves at construction — the repo
                             # must be cloned before the loop starts.
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
  # webhook_secret: "..."    # set before exposing /webhook/pr publicly
```

## Use

- **Headless / via the agent:** `board_create_epic`, `board_create_feature`
  (`title`, `spec`, `acceptance_criteria`, `files_to_modify`, `depends_on`, …),
  `board_mark_ready`, `board_list`.
- **Plan a project:** the `decompose-project` skill ("decompose <idea>") runs the
  adversarial pipeline and populates the board.
- **HTTP API** (`/plugins/project_board/*`): `epics`, `milestones`, `features`,
  `features/{id}/{ready,dep,block,unblock,ci}`, and `/webhook/pr` (the Done edge —
  a stable public URL GitHub posts to; ungated so GitHub, which can't send a bearer,
  reaches it). `features/{id}/{cancel,test-rung}` and `DELETE features/{id}` are
  **operator-only** — no `@tool` wrapper, so the board's own lead agent has no way
  to call them.
- **Watch it:** the **Board** console view (left-rail) at
  `/plugins/project_board/board` — Kanban + list, live-refreshing, served by the
  same router as the API (so the declared view path is genuinely mounted).

## Layout

| File | What |
|---|---|
| `store.py` | the `br`/beads wrapper — board projection + the Ready/Done invariants |
| `loop.py` | the puller: `ready → worktree → coder → PR → in_review` (+ opt-in escalation) |
| `worktree.py` | per-feature worktree lifecycle, scoped coder dispatch, `open_pr` |
| `coder_seam.py` | the ADR 0064 P2 seam — dispatches a build through `coder.solve()` when available, else honest-degrades |
| `api.py` | the HTTP API + the `/webhook/pr` Done edge (HMAC-verified) |
| `board_view.py` | the Kanban/list console view |
| `retro.py` | loop-retro mining: bead attempt/outcome history → recurring failure classes (the self-improving flywheel) |
| `subagents.py` + `skills/` | the `decompose`/`antagonist` planning layer + the `loop-retro` distill skill |
| `__init__.py` | `register()` — wires it all |

Ships **disabled**; nothing runs until you enable it and declare a coder.

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
