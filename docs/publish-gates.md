# Publish gates and the release freeze

Two guards for work that crosses repos.

- **Publish gates (`waits_for`)** keep a card out of the claim until something outside the
  board has happened: a version carrying a specific change is on npm, a GitHub release
  exists, a PR is merged.
- **The release freeze** keeps the auto-merge edge from merging into a repo that is in the
  middle of cutting a release.

## Why `depends_on` is not enough, and why a version floor is not either

`depends_on` and `foundation` release a dependent card when its blocker **merges**. In a
package chain that is too early. A design-system change in protoContent merges. The
changesets bot then opens or refreshes the "chore: release packages" PR, and only when
*that* PR merges does the new `@protolabsai/ui` reach npm.

A **version floor** doesn't close the gap either. A card that waits for
`npm:@protolabsai/ui@>0.62.0` can be released by the wrong publish. Say the Version PR is
already open when the consumer card is written (protoContent#528 was, during the review
of this feature). It can merge first and publish `0.62.1` **without** the design-system
change. The floor is met and the consumer's coder installs a version that lacks the API
it needs. Guessing the bump (`>=0.63.0`) fails the same way whenever the pending release
already carries a minor.

So the gate a consumer needs names the **change**, not a version: `contains:`.

## Spec grammar

A card carries a comma-separated list of specs. **All** of them must hold.

| Spec | Holds when |
|---|---|
| `npm:<package>@contains:<owner>/<repo>@<card-id>` | the newest published version is **proven** to contain that card's merge commit (unmet until the card's PR has merged) |
| `npm:<package>@contains:<owner>/<repo>@<sha>` | the same, for a commit you name |
| `npm:<package>@<semver-range>` | a published, non-deprecated version satisfies the range |
| `npm:<package>` | any non-deprecated version is published (prereleases count) |
| `release:<owner>/<repo>@<tag>` | that git tag exists |
| `release:<owner>/<repo>@<package>@<semver-range>` | a published release tagged `<package>@x.y.z` satisfies the range (changesets monorepos) |
| `release:<owner>/<repo>@<semver-range>` | a published release's tag (`v1.2.3` / `1.2.3`) satisfies the range, for repos that tag plain versions only |
| `pr:<owner>/<repo>#<n>` | that PR is merged |

- Scoped packages work as written: `npm:@protolabsai/ui@>=0.63.0`,
  `release:protoLabsAI/protoContent@@protolabsai/design@>=0.9.3`.
- Ranges follow node-semver (`>=`, `<`, `^`, `~`, `1.x`, hyphen ranges, `||`), including
  its prerelease rule. `>=0.63.0` is **not** satisfied by `0.64.0-next.1`. The matcher
  was checked against node-semver itself on over 100k random version/range pairs, with
  zero mismatches.
- Release ranges skip drafts and GitHub **prereleases**, and read every page of releases.
- A **bare** `release:` range on a repo that tags per package is never met, because any
  package's version would satisfy it. `board_create_feature` / `board_update_feature`
  refuse it and name the qualifier syntax.
- There is no `card:` kind. A card waiting on another card's merge is `depends_on`.
- A spec that doesn't parse is refused at create/update. One that reaches a card's notes
  anyway (hand-edited) reads unmet with its error. It never stops the loop or a listing.

### How `contains:` proves it

npm doesn't record which commit a pnpm/changesets publish came from. The packument has no
`gitHead` for `@protolabsai/ui` or `@protolabsai/design`, which was checked while building
this. What changesets does record is a git tag per version it publishes:
`@protolabsai/ui@0.62.0`. So the gate:

1. resolves the anchor. A sha is used as is. A card id is looked up on the board. Its PR
   must be merged, and the PR's `merge_commit_sha` is the anchor. Until then the gate
   reads `card bd-a1 not merged yet (#501)`.
2. reads the packument and takes the newest non-deprecated stable version.
3. finds its tag in the anchor repo (`<package>@<version>`, else `v<version>`, else
   `<version>`) and dereferences an annotated tag to its commit.
4. asks GitHub `compare/<anchor>...<tag commit>`. `ahead` or `identical` means the
   published version contains the change.

Releases are cut from one linear `main`, so if the newest publish lacks the change, no
older one has it. The gate says what it saw:
`npm @protolabsai/ui containing protoLabsAI/protoContent@bd-a1 (latest 0.62.1, tag @protolabsai/ui@0.62.1 at b7c3…, lacks 4f1e…)`.

A published version without a tag reads unmet (`has no tag … to prove it`). The gate
never guesses.

Set gates with `waits_for` on `board_create_feature`, `board_create_task` and
`board_update_feature` (which **replaces** the list; `none` clears it), on
`POST /features` and `PATCH /features/{fid}` (`""` leaves them, `"none"` or `[]` clears),
and on a `create_from_plan` item.

## What the loop does

1. A card with gates goes `ready` like any other. The Ready gate does not look at them.
2. In the claim scan, a ready, dependency-free candidate that carries gates is checked
   before it is claimed. If any gate is unmet, the card is skipped with reason
   `waiting-on-publish`. That is never counted as a livelock, however long the wait.
3. The card says so in `board_list`, `board_get_feature` (with each gate's last verdict),
   `GET /features`, the console chip and `board_dispatch`
   (`held.waiting-on-publish`).
4. When every gate holds, the loop logs `publish gates cleared (…) — claimable`, comments
   the same on the card, and claims it in the same tick.

`board_check_gates` (or `POST /features/{fid}/gates/check`) runs the checks now.

`board_attach_pr` does **not** check gates. It attaches a PR someone already opened by
hand, and gates only govern whether the loop may *start* the work.

### Cost and failure

- Results are cached per spec and shared by every card that names it. An unmet gate is
  re-read at most every 120 s, a met one hourly, and an on-demand check never re-asks a
  spec checked in the last 15 s. Scheduling uses the monotonic clock.
- A `contains:` check costs about five reads: packument, PR, tag ref, tag object, compare.
- A failed check (registry down, GitHub rate limit, no auth) is **unmet**, shows
  `(check failed: <error>)`, and backs off from 60 s, doubling to a cap of 30 min.
- Only cards the loop could claim right now are checked.

### Credentials

- `npm:` reads the public registry anonymously. For a private package, set the
  `npm_token` secret, or `PROJECT_BOARD_NPM_TOKEN`, or `NPM_TOKEN`. npm answers an
  anonymous read of a private package with **404**, which looks like "never published".
  So a scoped package's 404 without a token says `(not published yet — or private: set
  project_board.npm_token)`.
- `release:`, `pr:` and `contains:` go through `gh api`, with the board's own `gh` login.
  For a private producer repo (protoContent is private), that login must be able to read
  it. A repo it can't read shows as `(check failed: … 404)` on the card.

### Where the specs live

On the bead's `notes` field, one `waits-for: <spec>` line per gate. Not in a label, because
beads caps labels at 50 characters (#353) and real specs are longer. Every writer of
`notes` carries the gates forward. A `files_to_modify` entry that starts with
`waits-for:` (or `req:` / `source-issue:`) is refused, so it can't turn into a gate.
`tests/test_publish_gate_real.py` round-trips a 70-character spec through real `br`.

## Worked example: a design-system token, published, then adopted

The design-system agent asks protoEngineer for a `--pl-color-accent-subtle` token and a
`subtle` Badge tone, adopted in protoAgent's console.

```yaml
project_board:
  auto_merge: true
  projects:
    protoContent:
      repo: ~/dev/protoContent
      release_freeze: false        # changesets: the Version PR is nearly always open
    protoAgent:
      repo: ~/dev/protoAgent       # release_freeze unset: the defaults apply
```

**Card 1, the change** (protoContent). It must ship a changeset, or nothing is published:

```
board_create_feature(
  project="protoContent",
  title="Add accent-subtle token and Badge subtle tone",
  spec="… add --pl-color-accent-subtle to packages/design; tone='subtle' on Badge …
        Ship .changeset/accent-subtle.md: minor for @protolabsai/design and @protolabsai/ui.",
  acceptance_criteria="- WHEN a Badge renders with tone='subtle' THE SYSTEM SHALL …",
  files_to_modify="packages/design/src/tokens.ts, packages/ui/src/Badge.tsx, .changeset/accent-subtle.md (new)",
)
→ bd-a1
```

**Card 2, the adoption** (protoAgent):

```
board_create_feature(
  project="protoAgent",
  title="Adopt the Badge subtle tone in the console",
  spec="Bump @protolabsai/ui in apps/web to the release that carries tone='subtle' …",
  acceptance_criteria="…",
  files_to_modify="apps/web/package.json, package-lock.json, apps/web/src/…/StatusBadge.tsx",
  waits_for="npm:@protolabsai/ui@contains:protoLabsAI/protoContent@bd-a1",
)
```

No version to predict and no `depends_on` needed. The gate is unmet until bd-a1's PR
merges, then until a published `@protolabsai/ui` is tagged at a descendant of that merge
commit. `depends_on="bd-a1"` is still fine to add for the ordering it shows on the board.

What happens, including the race that a version floor loses:

| Time | Card 2 reads |
|---|---|
| bd-a1 in review, Version PR #528 already open | `… (card bd-a1 not merged yet (#501))` |
| #528 merges first and publishes ui@0.62.1 **without** bd-a1 | same. A floor `>0.62.0` would have released it here |
| bd-a1 merges; the bot regenerates the Version PR | `… (latest 0.62.1, tag … at b7c3…, lacks 4f1e…)` |
| the Version PR merges; 0.63.0 published and tagged | claimed. `publish gates cleared (… (0.63.0 published, contains 4f1e…))` |
| card 2's PR green while protoAgent is cutting v0.173.0 | `held: release freeze (PR #3565 (prepare-release/v0.173.0))` |
| the release is tagged | merged by the next merge poll |

## The release freeze

Before the auto-merge edge merges a PR that is otherwise ready, it checks whether the PR's
repo is mid-release. If it is, the merge is held. The card stays `in_review` reading
`held: release freeze (<evidence>)`, with one comment on the card and no merge attempt
spent, and every merge poll checks again.

The signals, per project (`release_freeze`, see [configuration](configuration.md)):

| Signal | Default | Read |
|---|---|---|
| a remote branch matches | `prepare-release*` | `git ls-remote --heads origin` |
| an open PR's head matches | `prepare-release*` | `gh api repos/<o>/<r>/pulls?state=open` |
| a workflow has an active run | `prepare-release.yml` | `gh api …/actions/workflows/<file>/runs` (404 = no such workflow = not frozen) |
| base's head is an untagged release commit | `chore: release v*` | `gh api …/commits/<base>`, then the tag named in the subject |

The last signal covers the gap after the release PR merges and before its tag is pushed.
protoAgent deletes the merged `prepare-release/*` branch at once, so the branch and PR
signals are already quiet while the release workflow is still tagging.

**Failure handling:**

- A signal the credential **cannot read** (HTTP 403, e.g. a token without
  `Actions: read`) is skipped, not treated as frozen. The other signals still decide. The
  loop logs a named warning once, and the setup status carries a `release_freeze`
  advisory ("partly blind: …") until the process restarts.
- Any **other** failure (GitHub down, rate limited) holds the merge with the error as
  evidence and retries next poll. A delayed merge costs one poll interval; a mid-release
  merge costs the release's whole check run.

Defaults per repo type:

| Repo | `release_freeze` | Why |
|---|---|---|
| protoAgent-style (`prepare-release.yml` → `prepare-release/vX.Y.Z` PR → tag) | unset (default) | every merge during that window restarts the release checks, about 15 min |
| changesets (protoContent) | `false` | the Version PR is open whenever any changeset is pending, and merging other PRs just folds their changesets in |
| keeps release branches after merging | `{pr_heads: [...], workflows: [...], release_commits: [...]}` | a branch glob would freeze forever |
| no release process | unset | nothing matches, so nothing is held |
