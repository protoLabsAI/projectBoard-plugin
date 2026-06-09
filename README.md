# Project Board — coding orchestration plugin

A **protoAgent plugin** that turns an idea into merged PRs: a lean 6-state board
backed by [beads](https://github.com/steveyegge/beads) (`br`), an **ACP spawn loop**
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
  it → commits/pushes → opens a PR → `in_review`. A **merge webhook** is the single
  edge that sets `done` (and reaps the worktree).
- **DAG + gates** — `depends_on` are `blocks` edges; a dependent stays out of the
  puller until its blocker is **merged** (foundation merge-gate). The **Ready gate**
  requires a spec, EARS acceptance criteria, and explicit `files_to_modify`.
- **Escalation (opt-in)** — with a `coders` map of >1 distinct delegate, a capability
  failure climbs a model tier (`fast→smart→reasoning`) and blocks at the top.
- **Planning layer** — two reasoning subagents (`decompose` + `antagonist`) driven by
  the `decompose-project` skill: idea → outline → MADR ADRs → epics › milestones ›
  features, hardened by an adversary, with a per-epic human gate.
- **Console view** — a Kanban + list projection over the `/features` API (ADR 0026).

It **composes** the upstream `delegates` plugin (ADR 0024/0025) for the ACP/A2A
spawn primitive — it does not reimplement it.

## Requirements

- **protoAgent ≥ 0.27.0** (console views + the ACP delegate teardown).
- The **`br`** (beads) CLI on `PATH` — the board's DAG/status store.
- `git` + the **`gh`** CLI (authenticated) for branch push + PR creation.
- The **`delegates`** plugin enabled, with an **`acp`** coder delegate declared
  (e.g. `proto`). A reviewer `a2a` delegate is optional (review dispatch is off by
  default — most fleets review PRs via a pipeline on open).

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
  coder: proto
  repo: ~/dev/my-repo
  base_branch: main
  loop_enabled: false        # flip true to start the background puller
  # webhook_secret: "..."    # set before exposing /webhook/pr publicly
```

## Use

- **Headless / via the agent:** `board_create_epic`, `board_create_feature`
  (`title`, `spec`, `acceptance_criteria`, `files_to_modify`, `depends_on`, …),
  `board_mark_ready`, `board_list`.
- **Plan a project:** the `decompose-project` skill ("decompose <idea>") runs the
  adversarial pipeline and populates the board.
- **HTTP API** (`/plugins/project_board/*`): `epics`, `milestones`, `features`,
  `features/{id}/{ready,dep,block,unblock,ci}`, and `/webhook/pr` (the Done edge).
- **Watch it:** the **Board** console view (left-rail) — Kanban + list, live-refreshing.

## Layout

| File | What |
|---|---|
| `store.py` | the `br`/beads wrapper — board projection + the Ready/Done invariants |
| `loop.py` | the puller: `ready → worktree → coder → PR → in_review` (+ opt-in escalation) |
| `worktree.py` | per-feature worktree lifecycle, scoped coder dispatch, `open_pr` |
| `api.py` | the HTTP API + the `/webhook/pr` Done edge (HMAC-verified) |
| `board_view.py` | the Kanban/list console view |
| `subagents.py` + `skills/` | the decompose/antagonist planning layer |
| `__init__.py` | `register()` — wires it all |

Ships **disabled**; nothing runs until you enable it and declare a coder.
