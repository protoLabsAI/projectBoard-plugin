# Configuration reference

Every key the board reads, with its default and when a change takes effect.
`tests/test_docs_reference.py` fails if the code starts reading a key this file does not
list — so an undocumented knob cannot be added quietly.

## How a change takes effect

| Applies | Meaning |
|---|---|
| `live` | picked up by the running loop on the next tick, no restart |
| `reload` | read when the plugin reloads (a settings save reloads it) |
| **`restart`** | the loop reads it ONCE at start — the process must restart |

**`· YAML only`** marks a key the Settings UI cannot edit: it is absent from
`protoagent.plugin.yaml`'s schema, so `POST /api/settings` refuses it and the console
never renders it. **21 of 65 keys are in this state, including `coders` and `projects`** —
the two you must set for a multi-repo board. Edit
`~/.protoagent/<instance>/config/langgraph-config.yaml` directly, then restart.

> A restart knob edited by hand is invisible until the restart — `loop_cfg_stale` compares
> the routers' config against the loop's snapshot, and a hand edit updates neither. The
> board will report healthy while running the old value.

## Minimum viable board

```yaml
project_board:
  loop_enabled: true
  repo: /path/to/your/checkout
  base_branch: main
  coder: my-acp-delegate       # must be a declared `acp` delegate
  local_gate_cmd: make test    # run before a PR opens
```

That is enough to pull a `ready` card, build it in a worktree and open a PR. Everything
below tunes it.

## Getting it running

You cannot dispatch a card without these.

| Key | Default | Applies |
|---|---|---|
| `loop_enabled` | `False` | reload |
| `repo` | `"."` | **restart** |
| `base_branch` | `"main"` | **restart** |
| `coder` | `—` | live |
| `coders` | `—` | **restart** **· YAML only** |
| `db_path` | `—` | **restart** |
| `br_autofetch` | `True` | live |

## Multi-project

One board, several repos. Each entry carries its own repo, gate and ladder, so repo and gate can never drift apart.

| Key | Default | Applies |
|---|---|---|
| `projects` | `—` | reload **· YAML only** |
| `project` | `—` | **restart** |
| `default_project` | `—` | reload **· YAML only** |
| `repo_conventions` | `""` | reload **· YAML only** |
| `gate_files` | `—` | reload **· YAML only** |

`project` picks ONE entry out of the host's projects registry and layers it under the
board's own settings — it is a restart knob because the entry supplies `repo` and
`base_branch`, which the loop reads once at start. Naming an entry that the registry does
not have is a hard error, not a fallback.

`default_project` names the entry a card lands in when `board_create_feature` is called
without a `project`. Leave it unset with exactly one project configured and that one is
used; leave it unset with several and the caller must name one.

## The gate — the coder's fast slice of CI

Run before a PR opens, so a failure costs a fix round instead of a CI round-trip.

| Key | Default | Applies |
|---|---|---|
| `local_gate_cmd` | `""` | reload |
| `local_gate_max` | `2` | reload |
| `local_gate_output_chars` | `4000` | reload **· YAML only** |
| `format_cmd` | `""` | reload **· YAML only** |
| `preflight` | `True` | reload **· YAML only** |
| `preflight_timeout_s` | `self.local_gate_timeout` | reload **· YAML only** |

## Dispatch and escalation

How a card becomes a build.

| Key | Default | Applies |
|---|---|---|
| `coder_timeout_s` | `1800` | reload |
| `empty_result_max` | `2` | reload **· YAML only** |
| `max_mode_n` | `1` | reload |
| `ready_skip_max` | `_READY_SKIP_MAX_DEFAULT` | reload **· YAML only** |

## coder.solve() search (ADR 0064)

Generate K candidate implementations and verify each against a real test command.

| Key | Default | Applies |
|---|---|---|
| `coder_solve` | `True` | reload |
| `coder_solve_k` | `3` | reload |
| `coder_solve_test_cmd` | `""` | reload |
| `coder_solve_test_timeout_s` | `300` | reload |
| `coder_solve_fusion_delegate` | `""` | reload **· YAML only** |
| `coder_solve_fusion_k` | `2` | reload **· YAML only** |
| `coder_solve_fusion_max_file_chars` | `coder_seam.FUSION_MAX_FILE…` | reload **· YAML only** |
| `coder_solve_fusion_max_total_chars` | `0` | reload |

## Review and merge

The gates between a green build and main.

| Key | Default | Applies |
|---|---|---|
| `review_gate` | `False` | reload |
| `review_dispatch` | `False` | reload |
| `reviewer` | `"quinn"` | reload |
| `merge_method` | `"squash"` | reload |
| `merge_poll` | `True` | reload |
| `auto_merge_max` | `3` | reload |
| `merged_verify_max` | `5` | reload |
| `ci_fix_max` | `2` | reload |
| `review_fix_max` | `2` | reload |
| `auto_merge` | `False` | live |

## Housekeeping

| Key | Default | Applies |
|---|---|---|
| `archive_after_days` | `7` | reload **· YAML only** |
| `max_files_by_difficulty` | `—` | reload **· YAML only** |
| `kg_lessons` | `True` | reload **· YAML only** |
| `kg_lessons_k` | `3` | reload **· YAML only** |
| `kg_lessons_domain` | `"loop-lessons"` | reload **· YAML only** |

## Concurrency

How much the loop runs at once. All three are **live** — the running loop picks them up on
its next tick, so you can throttle a board that is running hot without a restart.

| Key | Default | Applies |
|---|---|---|
| `max_concurrent` | `1` | live |
| `max_pending_reviews` | `5` | live |
| `max_concurrent_sessions` | `0` | live |

`max_concurrent` is the number of cards in flight at once (floor 1) — one per project is
the usual setting for a multi-repo board. `max_pending_reviews` caps how many cards may sit
in review before the loop stops claiming new ones (0 = no cap), which is what stops a
review backlog from starving the queue.

`max_concurrent_sessions` caps ACP sessions, and it is the one to reach for first when the
board overwhelms a local gateway. It is **0 = uncapped** by default, and the ceiling is not
`max_concurrent`: each dispatched card opens up to `coder_solve_k` candidate sessions, so
peak concurrency is `max_concurrent × coder_solve_k`. The loop says so at boot:

```
coder_solve_k=3: peak concurrent ACP sessions = max_concurrent × coder_solve_k = 6 × 3 = 18
(set max_concurrent_sessions to cap this)
```

Set it to `1` to serialise candidates within a card while still building several cards
in parallel.

## Everything else

| Key | Default | Applies |
|---|---|---|
| `auto_rebase` | `self.merge_poll` | reload |
| `ci_poll` | `self.merge_poll` | reload |
| `coder_solve_budget` | `6` | reload |
| `coder_solve_tree_depth` | `2` | reload |
| `dep_gate` | `"merge"` | reload |
| `env_passthrough` | `—` | **restart** **· YAML only** |
| `goal_fix_max` | `2` | reload |
| `goal_verify` | `False` | reload |
| `health_sweep_interval_s` | `300` | reload |
| `local_gate_timeout_s` | `600` | reload |
| `loop_interval_s` | `30` | reload |
| `merge_poll_interval_s` | `60` | reload |
| `rebase_fix_max` | `1` | reload |
| `review_run_max` | `3` | reload |
| `review_workflow` | `"code-review"` | reload |
| `webhook_secret` | `—` | reload |
| `worktrees_root` | `".worktrees"` | reload |

Two of those carry security weight and are easy to skim past:

**`webhook_secret`** — the shared HMAC-SHA256 secret for the PUBLIC ingress routes
(`/webhook/pr` and the CI/review callbacks; GitHub supplies the signature, other callers
sign their exact JSON bytes). It falls back to `PROJECT_BOARD_WEBHOOK_SECRET` in the
environment. **Blank fails CLOSED** — those routes mutate board state, so they are refused
rather than opened just because the host happens to be reachable only on loopback.

**`env_passthrough`** — the whitelist of environment variables that gate, format and coder
child processes are allowed to see (#86). The board strips the host's identity/credential
block from every child environment; this is the escape hatch for a deployment that
genuinely needs one of them (a private index token, say). The whitelist WINS, so anything
named here is passed through — keep it as short as the build actually requires.

## The two that are easy to get wrong

**`coders`** — the capability ladder, tier → delegate. A rung may hold SEVERAL
interchangeable providers, and the board round-robins across them and fails over on a rate
limit:

```yaml
coders:
  smart: [codex, sonnet]   # small/unset difficulty starts here
  reasoning: opus          # medium/large start here
  opus: opus               # architectural starts here
```

Climbing a rung means "a stronger model may succeed". Rotating within one means "this
model is fine, its quota is not". A card's STARTING rung comes from its difficulty, so a
`medium` card never touches rung 1.

**`projects`** — one board, several repos. Each entry carries that repo's own `repo`,
`base_branch`, `local_gate_cmd` and `coders`, so a card is built and gated against the
repo it belongs to. Without it, every card uses the top-level values.
