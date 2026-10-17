# Requirements and install

Get the plugin running. For the config keys see [annotated-config.md](annotated-config.md) and the [configuration reference](configuration.md).

## Requirements


- **protoAgent ≥ 0.185.0**: tabbed plugin Configure dialogs and sandboxed custom
  Configure views (protoAgent #3179/#3180, 0.153.2), and the callable
  `HOST.apply_settings` that runs a read-modify-write inside the host's config write lock
  (#2743, 0.164.0). Registering a board project merges the host's `projects:` list, and
  only that lock keeps the merge from dropping a concurrent writer's entry (#452).
  And `infra.clock.wait_for` (#3724, 0.185.0), which bounds every coder dispatch by
  `coder_timeout_s` counting time the machine spends asleep (#472).
- **beads-rust** — the **`br`** CLI, the board's DAG/status store. **Fetched for you on
  first run (v0.43.0)**: with no `br` on `PATH` the plugin downloads the pinned release
  (`br_fetch.BR_VERSION`, sha256-verified per platform) into the instance's plugin-data
  dir and uses it — see [br fetched on first run](setup-preflight.md#br-fetched-on-first-run-v0430). To install by hand:
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
the **[setup preflight](setup-preflight.md)** at register time and every loop tick, so a host that
is missing one says so instead of booting green.

## Install


```bash
python -m server plugin install https://github.com/protoLabsAI/projectBoard-plugin --ref main
```

`install` deliberately does **not** enable — installing is fetching code, enabling is
trusting it, and they are separate decisions. Enable it either way:

```bash
# In the console: Settings → Plugins → Project Board → enable. Enabling is fully LIVE —
# tools, subagents and the plugin's router (which serves the board view) hot-mount on the
# same reload, so the board works immediately with no restart.

# Or from the API, if you are scripting a setup:
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"enabled": true}' \
     http://127.0.0.1:7870/api/plugins/project_board/enabled

# Or by hand: add `project_board` to `plugins.enabled` in the YAML (see annotated-config.md), then restart.
```

Next: [annotated-config.md](annotated-config.md) has a commented `project_board:` block with every common key.
