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
never renders it. **26 of 72 keys are in this state, including `coders` and `projects`** —
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
| `setup_cmd` | `""` | reload **· YAML only** |
| `setup_timeout_s` | `600` | reload **· YAML only** |
| `preflight` | `True` | reload **· YAML only** |
| `preflight_timeout_s` | `self.local_gate_timeout` | reload **· YAML only** |
| `preflight_cmd` | `""` | reload **· YAML only** |

**`setup_cmd`** — installs a fresh worktree's own dependencies before its coder starts,
e.g. `npm ci --no-audit --no-fund --prefer-offline`. Runs in every worktree the board makes
for a build: the card's tree, each Max-Mode and ladder candidate, and the merged-state verify
tree. Without it, a tree borrows the repo checkout's installed `node_modules` through
symlinks, which is cheap but hands every card whatever that checkout last installed. Set it
per project (a `projects:` entry) or board-wide. Bounded by `setup_timeout_s`: a failed or
hung install is killed, logged, and the work goes ahead without it.

**The gate preflight** smokes each project's check on its clean base checkout before any
of that project's cards dispatch (`preflight: true`, the default). A red result holds the
project's ready cards until the check passes again. How it runs:

- **One run per project at a time.** The tick and an on-demand `board_dispatch` both
  preflight before they claim. A second caller waits for the run already in flight
  instead of starting another gate in the same checkout (#456).
- **One verdict per commit.** A pass, or a run that timed out, stands for the commit the
  checkout was on. When the checkout moves, one re-check runs in the background while
  dispatch goes on under the old verdict. A red result holds the project from the next
  claim scan. A failed project is re-checked every minute or so, as before.
- **A check slower than the timeout runs once.** A preflight cut off by
  `preflight_timeout_s` gives no verdict, and dispatch is allowed. It is not repeated
  until the checkout moves. It is logged once, and shown in `/status` under
  `preflight.slow` and in the setup advisories (`preflight_hint`). The duration also
  feeds the coder.solve() oracle guard below.

**`preflight_cmd`**: a cheap command for the preflight to smoke instead of the full
`local_gate_cmd`, such as `ruff check . && lint-imports`. The preflight asks "can this
environment run the repo's tools?", and lint plus an import check answers that in seconds.
A 12-minute test suite behind a 600 s timeout never answers it at all. Set it per project
in a `projects:` entry. The top-level value applies only to a project with no
`local_gate_cmd` of its own, so a check written for one repo never runs in another. Blank
means the preflight smokes `local_gate_cmd`, as before.

## Card authoring — the Ready gate

What `board_mark_ready` checks before a card can be pulled, and what `board_create_feature`
/ `board_update_feature` dry-run the moment a card is written (#455).

| Key | Default | Applies |
|---|---|---|
| `max_files_by_difficulty` | `—` | reload **· YAML only** |
| `breadth_exclude` | `DEFAULT_BREADTH_EXCLUDE` | reload **· YAML only** |
| `hot_files` | `[]` | reload **· YAML only** |

**`max_files_by_difficulty`** — the breadth cap: the most COUNTED `files_to_modify` a card
of each difficulty may name (built-in `{small: 4, medium: 4, large: 6}`; `architectural`
is uncapped and answers to the design gate instead). A wider card times out before it
lands, so the gate asks for a split.

**`breadth_exclude`** — globs for files nobody authors, which stay in the card (the coder
brief still lists them; the PR must carry them) but do not count toward the cap. Without
it a one-token change in a changesets monorepo — `src/tokens.js`, the committed
`dist/tokens.css` and `dist/tokens.json`, and the mandatory `.changeset/*.md` — is four
files and already at the medium cap. The default list:

```
.changeset/**   pnpm-lock.yaml   package-lock.json   yarn.lock   uv.lock   poetry.lock
Cargo.lock   **/dist/**   *.generated.*   CHANGELOG.md   changelog.d/**
```

Globs read like `.gitignore` lines. A glob with no `/` (other than a trailing one) matches
at any depth, and a leading `/` anchors it at the repo root. `**` spans directories. A glob
that matches a directory covers everything under it, so a bare `dist` and `dist/` both
match `pkg/dist/x.js`. A `(new)` marker and a leading `./` on a path are ignored. A configured list REPLACES the default. Put the word `defaults` in it
to keep the built-in globs and add your own:

```yaml
projects:
  protoContent:
    repo: ~/dev/protoContent
    breadth_exclude: [defaults, "packages/*/src/generated/**"]
```

`[]` counts every file. Set it in a `projects:` entry, or at the top level as every
project's fallback.

**`hot_files`** — globs for files nearly every card in a repo touches (`package.json`, a
barrel `index.ts`), which would otherwise need a `depends_on` edge on every new card. When
`board_create_feature` creates a card naming a hot file, the board adds a `depends_on` edge
onto the open card at the end of that file's chain: among the cards created before it,
the one no other holder depends on, with the latest created breaking a tie. It notes the
edge on the card and reports it as `hot_file_chain`. Creates racing in one process are
serialised; a cycle across processes is refused by `br` and reported as the edge's
`error`.
Cards on one file then form a chain in creation order as they are written. Empty by default:
nothing is chained you didn't ask for. A batch `POST /features/batch` plan states its own
order and is not auto-chained. Per project, or top level as the fallback:

```yaml
projects:
  protoContent:
    repo: ~/dev/protoContent
    hot_files: ["packages/*/package.json", "packages/ui/src/index.ts"]
```

The shared-file check itself needs no config. Two open cards of one project naming the same
file must be ordered by a `depends_on` PATH, in either direction, through open cards: a
chain `C → B → A` orders A and C as well (#458). The refusal names every unserialised pair
and the fewest edges that fix them: a chain with the furthest-along card first, then
creation order.

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
| `coder_solve_test_paths` | `—` | reload **· YAML only** |

**The oracle.** Each candidate is judged by `coder_solve_test_cmd`. When that is blank, it
falls back to the project's `local_gate_cmd`. Two guards stop a slow oracle from failing
every card (#459):

- **An unwinnable fallback turns solve() off.** When the oracle is the gate fallback and
  the preflight measured the gate at longer than `coder_solve_test_timeout_s`, that
  project's cards take the plain coder path. The pre-PR gate still runs. The loop logs
  this once, and `/status` shows it under `preflight.unwinnable_oracle`. It turns back on
  once you set `coder_solve_test_cmd` or `coder_solve_test_paths`, or the gate gets faster.
- **Two candidates timing out on the same command blocks the card.** The class is
  `oracle-timeout`, and there is no tier climb. A timeout says nothing about the code, so
  another candidate or a stronger model would only time out again. The sweep never clears
  this class on its own. Fix the oracle, then unblock the card. When the command was the
  gate fallback, the project's later cards skip solve() as well.

**`coder_solve_test_paths`**: judge each candidate by the tests for the files it
changed. The value maps path globs to commands, per project or board-wide:

```yaml
projects:
  protoAgent:
    coder_solve_test_paths:
      "apps/web/**": "npm ci --prefer-offline && (cd apps/web && npx tsc --noEmit) && npm run test:unit --workspace @protoagent/web"
      "docs/**": ""        # no test for docs-only changes
      "**": gate           # everything else: the project's local_gate_cmd
```

- Globs are fnmatch-style against the repo-relative path. `*` also crosses `/`, so
  `apps/web/*` and `apps/web/**` are the same.
- For each changed file, the **first** matching entry wins. The distinct commands picked
  run one after another, each in its own subshell, and the candidate passes only if all of
  them pass. They share one `coder_solve_test_timeout_s`.
- `gate` means the project's `local_gate_cmd`. `""` (or `skip`) means no test for those
  files. A candidate whose every changed file hits a skip entry passes without a command.
- A file no entry matches uses the ordinary oracle (`coder_solve_test_cmd`, else the
  gate). A candidate that changed nothing, or one whose files git can't list, also uses
  the ordinary oracle.
- "Changed" means changed against the point the candidate forked from `base_branch`:
  commits, uncommitted edits and new files.
- A mapping keeps its order in YAML. A list of `[glob, command]` pairs also works.

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

## Publish gates and the release freeze

Two guards for cross-repo work. [Publish gates](publish-gates.md) has the full story and a
worked example.

| Key | Default | Applies |
|---|---|---|
| `release_freeze` | `—` | reload **· YAML only** |
| `npm_token` | `""` | **restart** |

**`release_freeze`** — before the auto-merge edge merges a PR, it asks the PR's repo
whether a release is in flight and, if so, holds the merge (the card stays `in_review`
reading `held: release freeze (<evidence>)`) until the freeze lifts. Set it in a
`projects:` entry (or top level, as every project's fallback):

- unset / `true` — the default patterns: an `origin` branch or an open PR head matching
  `prepare-release*`, an active run of `prepare-release.yml`, or base's head commit being
  a `chore: release v*` commit whose tag is not pushed yet (the window after the release
  PR merges — protoAgent deletes the branch at once). A repo with none of those is never
  frozen, so leaving it on costs four reads per otherwise-ready merge and nothing else.
- `false` — off. **Use it for a changesets repo such as protoContent**: its release is a
  bot-maintained "Version Packages" PR (`changeset-release/main`) that is open whenever any
  changeset is pending, and merging other PRs meanwhile just folds their changesets into
  it — a freeze on it would hold nearly every merge for nothing.
- a list — each item is a glob matched against remote branches AND open PR heads, except
  `workflow:<file>` items (or items ending `.yml`/`.yaml`), which name workflows whose
  active runs freeze, and `commit:<subject glob>` items, which name untagged release
  commits: `[release/*, workflow:release.yml, "commit:release v*"]`.
- a mapping — `{branches: [...], pr_heads: [...], workflows: [...], release_commits: [...]}`,
  each signal set separately (an absent key turns that signal off). Use it for a repo that
  KEEPS its release branches after merging, where a branch glob would freeze forever:
  `{pr_heads: [prepare-release*], workflows: [prepare-release.yml], release_commits: ["chore: release v*"]}`.

A signal the `gh` credential cannot read (HTTP 403 — e.g. a token without `Actions: read`)
is SKIPPED, not treated as frozen: the other signals still decide, the loop logs a named
warning once, and the setup status carries a `release_freeze` advisory naming it. Any other
freeze-check failure (GitHub down, rate limited) HOLDS the merge with the error as evidence
and retries next merge poll: a delayed merge costs one poll interval, a merge into a release
in flight costs the release's whole check run.

**`npm_token`** — a read token for `npm:` publish gates on PRIVATE packages. Blank reads
the public registry anonymously, which is all a public package (`@protolabsai/ui`) needs.
Stored in secrets.yaml; `PROJECT_BOARD_NPM_TOKEN`, then `NPM_TOKEN`, are read when it is
blank.

## Housekeeping

| Key | Default | Applies |
|---|---|---|
| `archive_after_days` | `7` | reload **· YAML only** |
| `decompose_after_timeouts` | `2` | **restart** |
| `kg_lessons` | `True` | reload **· YAML only** |
| `kg_lessons_k` | `3` | reload **· YAML only** |
| `kg_lessons_domain` | `"loop-lessons"` | reload **· YAML only** |

`decompose_after_timeouts` is the one that changes behaviour rather than tuning it. A coder
timeout on a fresh build is a **size** signal, not a capability one. It produces no diff and no
CI output, so a retry re-sends a near-identical prompt, and climbing the model ladder spends a
stronger model on a card that was never model-limited.

Only those timeouts count:

- A pre-first-token timeout is an infra fault that splitting would not fix. It never counts,
  and never asks.
- A timeout on a fix round doesn't count either: the card already has a PR, or the coder was
  fixing a kept worktree. A card that built in one dispatch is not too wide.
- The count clears when a build reaches review.

On the timeout that reaches this count, the loop parks the card (blocked class `too-wide`) and
files a `ready` task asking this agent to split it. The card does not climb another rung, and
the blocked sweep does not rebuild it, since that would only time out again, racing the split.
The operator is told once, and the block reason names the task, or says that none was filed.

The task gives the agent a fixed order, each step passing the gates the next one relies on:

1. Create the slices, left in backlog.
2. Re-point the card's dependents onto the slices they need.
3. Cancel the card.
4. Mark the slices ready.

The ask is made once per card. Unblocking a parked card resets its count, so a retry after
raising `coder_timeout_s` is a real attempt. Set `0` to switch the ask off and have the card
simply block, as it did before.

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
limit or on a provider that can't serve its model:

```yaml
coders:
  smart: [codex, sonnet]   # small/unset difficulty starts here
  reasoning: opus          # medium/large start here
  opus: opus               # architectural starts here
```

Climbing a rung means "a stronger model may succeed". Rotating within one means "this
model is fine, its provider is not" — a spent quota, or a provider that refuses the model
outright (retired, not offered on the account's plan, or needing a newer client; #420).
Only a coder DISPATCH failure counts; the same words in a reviewer's gap or a test's output
do not.

A provider that refuses its model is remembered for 30 minutes. Later cards start on a live
sibling instead of each paying a failed dispatch to rediscover it, and a quota failure on
the live sibling takes its ordinary backoff rather than rotating onto it. The mark is a
preference, not a verdict: once no live option is left — every other sibling has failed, or
the quota backoff is spent — a marked provider still gets its one real attempt before the
card blocks (the operator may have repointed it), and a dispatch it serves clears the mark. If every provider on the rung refused its model on this
card, the card blocks under `dispatch-infra` naming them, and does not climb: that is a
config problem, and a stronger rung would only hide it. If some were only rate-limited, it
blocks as `rate_limit`, which the sweep heals on its own.

Max-mode (`max_mode_n > 1`) follows the same rules. If every candidate raised, the loop takes
the edge of the most specific failure among them, exactly as it would for a single dispatch.
The precedence is:

1. a refused model, even beside a quota;
2. a quota;
3. a timeout;
4. a dispatch failure the loop does not retry;
5. a retryable one;
6. any other board error, as raised.

A raw error that is not a board error keeps the old "no diff" verdict. Only a fan-out where
at least one candidate ran and came back with nothing is guaranteed to be a capability failure
that climbs. A climb after a timeout is a fresh build, so the stronger rung fans out again.

A card's STARTING rung comes from its difficulty, so a `medium` card never touches rung 1.

Rotation needs escalation on, and escalation needs at least two DISTINCT rungs — a map with
a single rung (`coders: {smart: [codex, sonnet]}`) runs as a one-coder board using `coder`.

**`projects`** — one board, several repos. Each entry carries that repo's own `repo`,
`base_branch`, `local_gate_cmd` and `coders`, so a card is built and gated against the
repo it belongs to. Without it, every card uses the top-level values.
