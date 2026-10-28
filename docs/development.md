# Development

Contributor notes: external-seam coverage, the file layout, standalone scripts and releasing.

## External-seam coverage — REAL, EXEMPT, or honest debt

A function whose whole job is to make an external system do something is close to untested
when it is validated only against a mock of that system — that shape shipped three GREEN-
but-inert fixes in one night (#353, #354, #356). `tests/test_external_seams.py` makes that
gap impossible to add silently: every seam that shells `gh`/`git`/`br` is CLASSIFIED, by
AST rather than by name, as **REAL** (exercised against the real binary/API in the
integration tier), **EXEMPT: `<reason>`** (real coverage genuinely not warranted, reason
stated), or **UNCOVERED** (honest debt) — and the UNCOVERED count is a ratchet that may
fall, never rise.

`worktree.py` reached its final contract over #361: **30 REAL / 3 EXEMPT / 0 UNCOVERED**
(`MAX_UNCOVERED_WORKTREE = 0`). The 11 local-git seams run against a real bare-origin +
clone (slice 1), as do #405's stranded-work seams (`preserve_worktree` and three helpers) and #427's
`commits_ahead` and `own_worktree`. The 12
read-dominant `gh` seams run against a pinned, permanently-open PR with `PB_REQUIRE_GH=1`
so an absent credential FAILS rather than skips (slice 2), as does #402's `pr_identity`.

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
| `board_cards.py` + `component/` | board cards in chat (#522): the `board-cards` frame component, its validator and page |
| `gates.py` | publish gates (`waits_for`): spec grammar, a stdlib node-semver range matcher, the npm / `gh` evaluators, the shared per-spec cache |
| `release_freeze.py` | the auto-merge edge's release-freeze guard: per-project patterns, the per-repo check, the per-card holds |
| `external_review.py` | an external QA panel's verdict on a PR (#473): its config, the FAIL-at-head judge over one `gh pr view` read, and the findings a fix round leads with |
| `review_coverage_hold.py` | `require_complete_review`: the per-card hold while the panel's pass at the head is incomplete, and the once-per-head `@vera review` summon |
| `retro.py` | loop-retro mining: bead attempt/outcome history → recurring failure classes (the self-improving flywheel) |
| `subagents.py` + `skills/` | the `decompose`/`plan_antagonist` planning layer + the `onboard-project`, `decompose-project`, `cross-repo-chain`, `loop-retro`, `showing-board-cards`, `card-from-chat` and `board-tool-reference` skills (the last holds the tool-description detail the `board_*` schemas leave out) |
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
spec = importlib.util.spec_from_file_location(
    "project_board", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
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
