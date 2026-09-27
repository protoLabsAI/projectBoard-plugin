# Publish gates and the release freeze

Two guards for work that crosses repos.

- **Publish gates (`waits_for`)** keep a card out of the claim until something outside the
  board has happened: a version is on npm, a GitHub release exists, a PR is merged.
- **The release freeze** keeps the auto-merge edge from merging into a repo that is in the
  middle of cutting a release.

## Why `depends_on` is not enough

`depends_on` and `foundation` release a dependent card when its blocker **merges**. In a
package chain that is too early. A design-system change in protoContent merges, then the
changesets bot opens a "chore: release packages" PR, and only when *that* PR merges does
the new `@protolabsai/ui` reach npm. A consumer card released at the first merge sends its
coder to install a version that does not exist yet. The coder fails, or worse, pins a
workaround.

A publish gate names the fact the consumer actually needs, and the loop checks it.

## Spec grammar

A card carries a comma-separated list of specs. **All** of them must hold.

| Spec | Holds when |
|---|---|
| `npm:<package>@<semver-range>` | the npm registry has a published version satisfying the range |
| `npm:<package>` | any version is published |
| `release:<owner>/<repo>@<tag>` | that git tag exists on GitHub |
| `release:<owner>/<repo>@<semver-range>` | a published (non-draft) GitHub release's tag satisfies the range |
| `pr:<owner>/<repo>#<n>` | that PR is merged |

- Scoped packages work as written: `npm:@protolabsai/ui@>=0.63.0`. The range starts after
  the **last** `@`.
- Ranges follow node-semver: `>=1.2.3`, `>1.2.3 <2.0.0-0`, `^0.63.0`, `~1.2`, `1.x`,
  `1.2.3 - 2.0.0`, and `||` between alternatives. No commas inside a spec.
- A **prerelease** satisfies a range only when the range names a prerelease on the same
  `major.minor.patch`, as in npm. `>=0.63.0` is **not** satisfied by `0.64.0-next.1`, so a
  snapshot publish never releases a consumer card.
- After `release:…@`, text that starts with `< > = ^ ~`, is `*`, contains a space or
  `||`, or has an `x` part is a range. Anything else is an exact tag name, so write the
  tag exactly (`v0.63.0`, not `0.63.0`, when the repo's tags carry the `v`). Release tags
  like `@protolabsai/ui@0.63.0` are read as `0.63.0` when matched against a range.
- There is no `card:` kind. A card waiting on another card on this board is `depends_on`.
- A spec that does not parse refuses the create or update, with the reason, so a card can
  never carry a gate that could not be met.

Set them with `waits_for` on `board_create_feature`, `board_create_task` and
`board_update_feature` (which **replaces** the list; `none` clears it), on the
`POST /features` and `PATCH /features/{fid}` routes, and on a `create_from_plan` item.

## What the loop does

1. A card with gates goes `ready` like any other. The Ready gate does not look at them.
2. In the claim scan, a ready, dependency-free candidate that carries gates is checked
   before it is claimed. If any gate is unmet, the card is skipped with reason
   `waiting-on-publish`. This is not a livelock: the scan never flags the card blocked for
   it, however long the wait.
3. The card says so. `board_list`, `board_get_feature`, `GET /features` and the console
   chip show

   ```
   waiting on publish: npm @protolabsai/ui >0.62.0 (latest 0.62.0)
   ```

   and each gate's last verdict (`gates: [{spec, met, detail, error, checked_at}]`).
   `board_dispatch` reports it under `held.waiting-on-publish`.
4. When every gate holds, the loop logs
   `bd-xyz publish gates cleared (npm @protolabsai/ui >0.62.0 (0.63.0 published)) — claimable`,
   comments the same on the card, and claims it in the same tick.

`board_check_gates` (or `POST /features/{fid}/gates/check`) runs the checks now.

### Cost and failure

- Results are cached **per spec**, shared by every card that names it. Twenty consumer
  cards waiting on one package cost one registry read per interval.
- An unmet gate is re-read at most every 120 s. A met one is re-read hourly.
- A failed check (registry down, GitHub rate limit, no auth) is **unmet**. The card shows
  `(check failed: <error>)`, and that spec backs off: 60 s, then 120 s, doubling to a cap
  of 30 min.
- An on-demand check skips the TTL but never re-asks a spec checked in the last 15 s.
- Only cards the loop could claim right now are checked. A backlog card, or one still
  waiting on `depends_on`, costs nothing until it gets there.

### Credentials

- `npm:` reads the public registry anonymously. For a private package, set the
  `npm_token` secret (Settings ▸ Project Board ▸ Security), or `PROJECT_BOARD_NPM_TOKEN`,
  or `NPM_TOKEN`. A 401/403 names the token in the card's error.
- `release:` and `pr:` go through `gh api`, with the same `gh` login the board uses for
  every PR.

### Where the specs live

On the bead's `notes` field, one `waits-for: <spec>` line per gate, beside
`files_to_modify`, the requirement ledger and the `source-issue:` line. Not in a label.
Beads caps a label at 50 characters and refuses the whole `br update` past it (#353), and
a real spec such as `npm:@protolabsai/ui@>=0.63.0 <1.0.0-0` is often longer and carries
characters (`/ @ < space`) the label validator rejects anyway. Every writer of `notes`
carries the gates forward. `tests/test_publish_gate_real.py` round-trips a 70-character
spec through real `br` and shows the label route is refused.

## Worked example: a design-system token, published, then adopted

The design-system agent asks protoEngineer for a new `--pl-color-accent-subtle` token and
a `subtle` Badge tone, adopted in protoAgent's console. `@protolabsai/ui` is at `0.62.0`.

The board has both repos as projects:

```yaml
project_board:
  auto_merge: true
  projects:
    protoContent:
      repo: ~/dev/protoContent
      base_branch: main
      release_freeze: false        # changesets: the Version PR is nearly always open
    protoAgent:
      repo: ~/dev/protoAgent
      base_branch: main
      # release_freeze unset: the default prepare-release patterns apply
```

**Card 1, the change** (protoContent):

```
board_create_feature(
  project="protoContent",
  title="Add accent-subtle token and Badge subtle tone",
  spec="… add --pl-color-accent-subtle to packages/design tokens; add tone='subtle' to Badge …
        Ship a changeset bumping @protolabsai/ui and @protolabsai/design (minor).",
  acceptance_criteria="- WHEN a Badge renders with tone='subtle' THE SYSTEM SHALL use --pl-color-accent-subtle …",
  files_to_modify="packages/design/src/tokens.ts, packages/ui/src/Badge.tsx, .changeset/accent-subtle.md (new)",
)
→ bd-a1
```

Merging it does not publish. The changesets action opens or refreshes the
"chore: release packages" PR, and merging that PR publishes to npm.

**Card 2, the adoption** (protoAgent):

```
board_create_feature(
  project="protoAgent",
  title="Adopt the Badge subtle tone in the console",
  spec="Bump @protolabsai/ui in apps/web and use tone='subtle' for …",
  acceptance_criteria="…",
  files_to_modify="apps/web/package.json, package-lock.json, apps/web/src/…/StatusBadge.tsx",
  depends_on="bd-a1",
  waits_for="npm:@protolabsai/ui@>0.62.0",
)
```

- `depends_on=bd-a1` keeps card 2 out of the claim until card 1 merges, and shows the order
  on the board.
- `waits_for=npm:@protolabsai/ui@>0.62.0` keeps it out until a version newer than today's
  latest is on npm. Every publish after card 1 merges carries card 1's changeset, because
  the Version PR is regenerated on every push to main. So "anything newer than 0.62.0,
  after card 1 merged" means "card 1 is published". You don't have to predict the bump. If
  you know it (a `minor` changeset), `>=0.63.0` says the same thing more precisely.
- Optional: once the bot's release PR exists (say `protoContent#219`), you can add
  `pr:protoLabsAI/protoContent#219` with `board_update_feature` to show the step on the
  card. The npm gate is the one that matters. A merged release PR whose publish job failed
  still leaves the card waiting, correctly.

What happens:

| Time | Card 2 reads |
|---|---|
| card 1 in review | (`depends_on` open; not yet checked) |
| card 1 merged, release PR open | `waiting on publish: npm @protolabsai/ui >0.62.0 (latest 0.62.0)` |
| release PR merged, publish job running | same, re-checked about every 2 min |
| 0.63.0 on npm | claimed. `publish gates cleared (… (0.63.0 published))` |
| card 2's PR green, protoAgent preparing v0.173.0 | `held: release freeze (PR #3565 (prepare-release/v0.173.0))` |
| release PR merged | merged by the next merge poll |

## The release freeze

Before the auto-merge edge merges a PR that is otherwise ready, it checks whether the PR's
repo is mid-release. If it is, the merge is held. The card stays `in_review`, reading
`held: release freeze (<evidence>)`, with one comment on the card. No merge attempt is
spent, and each merge poll checks again. When the freeze lifts, the loop logs
`release freeze lifted` and merges.

The signals, per project (`release_freeze`, see [configuration](configuration.md)):

| Signal | Default | Read |
|---|---|---|
| a remote branch matches | `prepare-release*` | `git ls-remote --heads origin` |
| an open PR's head matches | `prepare-release*` | `gh api repos/<o>/<r>/pulls?state=open` |
| a workflow has an active run | `prepare-release.yml` | `gh api …/actions/workflows/<file>/runs` (a 404 means no such workflow, not frozen) |

Defaults per repo type:

| Repo | `release_freeze` | Why |
|---|---|---|
| protoAgent-style: a `prepare-release.yml` that pushes `prepare-release/vX.Y.Z` and opens a PR | unset (default) | every merge during that window restarts the release checks, about 15 min |
| changesets (protoContent): a bot "Version Packages" PR on `changeset-release/main`, publish on merge | `false` | the Version PR is open whenever any changeset is pending. Merging other PRs folds their changesets in, which is harmless, and freezing on it would hold nearly every merge |
| a repo that keeps release branches after merging | `{pr_heads: [...], workflows: [...]}` | a branch glob would freeze forever |
| no release process | unset | nothing matches, so nothing is held |

A check that errors holds the merge, with the error as evidence, and retries next poll. A
delayed merge costs one poll interval. A merge into a release in flight costs the
release's whole check run.
