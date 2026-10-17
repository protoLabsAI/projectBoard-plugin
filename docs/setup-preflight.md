# Setup preflight — can the board run at all? (v0.42.0)

Distinct from the [gate preflight](gate.md#preflight-fail-closed) (which asks "can the *repo's tests* run"), the
setup preflight asks "can the *board* run": four checks, computed by
`setup_check.setup_status(cfg)` — pure, never raising, never a `br` board op (one
cached `br --version` at most):

| key | ok when | hint (operator copy) |
|---|---|---|
| `br` | the beads CLI resolves (`BR_BIN` > the auto-fetched binary > `br` on PATH) | "fetching beads-rust vX for <platform> …" while the auto-fetch runs; the download error + the install hint if it failed; the install hint if `br_autofetch` is off / `BR_BIN` is set but unresolvable / the platform has no build (Windows, musl) |
| `gh` | the GitHub CLI is on PATH | install it + `gh auth login`; builds can't open PRs until then |
| `coder` | **every** configured coder name (`coder`, the `coders` tier map, each `projects:` entry's `coders`) resolves to a live `acp` delegate — **no names configured is a failure**. With `coder` **blank**, the ladder is the only dispatch path: escalation must be on (>1 distinct delegate) and the instance map *and* every project map must cover **every** tier (`smart`/`reasoning`/`opus`) — an unmapped rung dispatches to `''` and blocks the card | roster-aware when no coder is set: with **zero** ACP delegates on the agent it says none can be selected yet and to declare one in core **Settings ▸ Capabilities ▸ Delegates** before naming it here (an empty board list is not a place to pick from); with some, it names them; with an unreadable roster it keeps the generic "pick a delegate in Settings ▸ Project Board or let the agent propose_delegate (the former implicit default `proto` no longer applies)". Also names the unresolvable delegate / the uncovered tier(s) |
| `repo` | the board is bound (explicit `repo`/`db_path`/`projects:`) to a directory that exists — or the shipped `repo: "."` default and the cwd already has a `.beads/` | add a repo from the board panel's **Projects ▸ Add project** (needs core Project onboarding on with an **Onboarding root** the repo resolves under — Settings ▸ Capabilities ▸ Project onboarding), or bind one directly with `project_board.repo` (absolute checkout path) / `db_path` / a `projects:` map |

Where it surfaces:

- **`GET /api/plugins/project_board/status`** → `setup: {br, gh, coder, repo, loop_enabled,
  loop_blockers, loop_cfg_stale, loop_cfg_stale_keys, loop_cfg_stale_hint, loop_restart_required,
  ready}` alongside the v0.40.0 `bound` keys. `loop_cfg_stale` is the reload-drift tell: a config
  reload rebuilds the routers on the NEW config while the running loop keeps its construction-time
  `coders` / `repo` / `base_branch` / `db_path` / `projects` — the status compares the two
  and says **"restart the agent to apply"** (on its own line, on the affected hint, and as
  the `loop` host warning) instead of reporting the new config as the loop's state.
  `coder` is live (applied by `reload()`), so it never goes stale. **`loop_restart_required`**
  is that drift made actionable: `true` only when the loop is configured **on** *and* running on a
  stale knob, so the "restart to apply" state is distinguished from a loop simply configured off
  (which has nothing running to restart). **`claim_stall`** (top level, and as
  `setup.claim_stall_hint`) is the #462 health gap: ready cards, a free drive slot, and no claim
  scan finished for `claim_stall_ticks` ticks. It names the tick phase that is stuck and is
  forwarded to the host as the `claim_stall` warning ([configuration](configuration.md#concurrency)).
- **The board page** renders each failing check with its hint (a warning card above the
  board, or in place of the raw error when the board can't be read at all).
- **Host operator warnings** — each failing check is forwarded to the host's
  `registry.report_setup_gap(key, message)` seam (keys `br`/`gh`/`coder`/`repo`, plus
  `loop` for the stale-config note; `None` clears it on recovery), which the console shows
  in `GET /api/runtime/status`. Edge-triggered after a first evaluation that sends every
  key unconditionally — so a reload's fresh reporter clears a warning the previous
  instance raised. Guarded: a host without the seam just gets the log lines.
  **Structured actions (feature-detected):** on a host whose seam takes the `action=`
  keyword (protoAgent ≥ v0.162.0:
  `report_setup_gap(key, message, *, label=None, action=None)`), the two *configuration*
  blockers — an unresolved `coder`, an unbound/invalid `repo` — are forwarded with an
  allowlisted `plugin_config` action
  (`{"kind": "plugin_config", "label": "Configure Project Board", "fields": ["coder"]}`, or
  `["repo"]`) so the console's setup-gap banner gets a button straight to Project Board's
  Configure dialog, the surface where the gap is actually resolved. The action only
  *navigates* there — it manufactures no coder and mutates no config (the host force-targets
  a `plugin_config` action at the reporting plugin). `br`/`gh` (PATH/install faults, fixed on
  the shell) and the advisories never carry that CTA, so it can't mislead. `GapReporter`
  detects the seam per instance (signature introspection, with a runtime fallback if the call
  is rejected), so an older host that exposes only
  `report_setup_gap(key, message, *, label=None)` (v0.146–v0.161) degrades to the plain hint
  string — same message text, key identity and edge-triggering either way. The tests drive
  the host's own `FakeRegistry` (`tests/_plugin_testkit.py`, protoAgent's testkit vendored
  verbatim), whose seam signatures the host keeps identical to the real registry — refresh it
  from `graph/plugins/testkit.py` when the host seam changes.
- **The loop pauses, it doesn't traceback.** With `loop_enabled: true` and a blocker
  standing (`br`, `coder`, `repo` — a missing `gh` only fails the PR edge, so it is
  reported but not paused on) the puller logs ONE `loop paused: …` warning and
  re-checks every `loop_interval_s` (off the event loop) — install `br`, declare the
  delegate, name it in the `coder` Settings field, bind the repo, and it runs crash
  recovery + starts ticking on its own. No restart. Before v0.42.0 the
  same board booted green and logged `crash recovery failed` + `loop tick failed`
  tracebacks every tick.

## br fetched on first run (v0.43.0)

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
