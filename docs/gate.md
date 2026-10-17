# The gate — the coder's fast slice of CI

The **pre-PR gate** (`local_gate_cmd`) is the command the loop runs in each coder's
worktree before opening a PR, so the coder's own solve-loop iterates to **green**
locally instead of shipping a PR that only fails in CI.

## Two tiers — the gate is NOT a full-CI replica

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

## `auto` — discover it, don't transcribe it

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

## Make your repo team-ready

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

## Preflight (fail-closed)

Before dispatching **any** work, the loop smoke-runs the resolved gate on the clean
base checkout (`preflight: true`, the default). If the gate can't even launch
(missing tool, broken deps, base already red) it **holds** all ready work — flagged
blocked, with the reason, visible on the board — rather than burn generations no coder
could pass, and re-checks each cycle so work resumes the moment it's fixed. A slow gate
that times out is treated as indeterminate → allowed (a slow gate must never wedge the
board). This is the fail-**closed** complement to the per-PR gate's fail-**open**: a
flaky gate never blocks good work, but an *unrunnable* gate never starts bad work.

One preflight runs per project at a time. A verdict stands for the checkout commit it was
reached on, and a timed-out run is not repeated on the same commit. A project can smoke a
cheaper `preflight_cmd` than its full gate. See
[docs/configuration.md](configuration.md#the-gate--the-coders-fast-slice-of-ci) (#456).

## Environment — what gate, format, and verify children see

Every subprocess the loop spawns to run repo-defined commands over coder-written
code — the gate preflight, the pre-PR `local_gate_cmd`, the auto-fix `format_cmd`,
the worktree install `setup_cmd`, and the `coder.solve()` acceptance-test (verify) run — receives a **narrow
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
