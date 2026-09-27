---
name: cross-repo-chain
description: >-
  Use when one piece of work has to land in one repo, be RELEASED or PUBLISHED (npm
  package, GitHub release, a release PR merged), and only then be picked up in another
  repo. The usual case is a design-system change in protoContent (published as
  @protolabsai/ui / @protolabsai/design) that protoAgent's console then adopts. Writes the
  chain as board cards whose consumer side waits on the publish (`waits_for`), not just
  on the merge (`depends_on`), and checks each repo's release_freeze setting. Plans cards,
  does not write code.
tools:
  - board_list          # what is already on the board (don't duplicate a card)
  - board_create_feature
  - board_create_task
  - board_update_feature  # add/replace a card's waits_for later (e.g. once the release PR exists)
  - board_get_feature   # read a card's gates and next_action back
  - board_check_gates   # check the gates now instead of at the loop's next sweep
  - board_mark_ready
  - read_file           # the producing repo's .github/workflows + .changeset/ (its release mechanism)
  - list_dir
---

# Cross-repo chains: change → publish → adopt

`depends_on` releases a card when its blocker **merges**. When the consumer needs the
change **published** (an npm version, a GitHub release), that is too early: the consumer's
coder would try to install a version that does not exist yet. Put the publish on the
consumer card as a **publish gate**:

```
waits_for="npm:@protolabsai/ui@>0.62.0"          # a version newer than 0.62.0 is on npm
waits_for="release:protoLabsAI/protoCLI@>=1.4.0" # a GitHub release >= 1.4.0
waits_for="pr:protoLabsAI/protoContent#219"      # that PR merged (e.g. the release PR)
```

A card may carry several, comma-separated, and all must hold. Until then the loop leaves
it unclaimed and the card reads `waiting on publish: npm @protolabsai/ui >0.62.0 (latest
0.62.0)`. The grammar and semantics are in `docs/publish-gates.md`.

## Steps

1. **Find the release mechanism of the producing repo.** Read its `.github/workflows/`.
   - **changesets** (protoContent): a PR with a `.changeset/*.md` merges, the changesets
     action opens or refreshes a "chore: release packages" PR, and merging THAT publishes
     to npm. The change card must ship a changeset, or nothing is ever published.
   - **tag/release workflow**: a `v*` tag or a `prepare-release` PR produces a GitHub
     release. Gate on `release:` rather than `npm:`.
2. **Read the current published version** before writing the gate (for example
   `0.62.0`). With a shell, `npm view @protolabsai/ui version`. Without one, the gate reports
   it: create the consumer card with your best floor (step 4), run `board_check_gates
   <id>`, and read `(latest 0.62.0)` in the gate's detail. Correct the spec with
   `board_update_feature` if needed. The loop won't claim the card while the gate is unmet,
   so a first guess is safe.
3. **Write the change card** in the producing project. Include the changeset file in
   `files_to_modify` (`.changeset/<slug>.md (new)`) and say which packages and which bump
   level in the spec.
4. **Write the consumer card** in the consuming project with BOTH:
   - `depends_on="<change card id>"`: ordering, visible on the board; and
   - `waits_for="npm:<package>@><current version>"`: the publish. Every publish after the
     change merges includes its changeset (the Version PR regenerates on every push), so
     "newer than today's latest, after the change merged" means "the change is published".
     You don't have to predict the bump. If you know the bump level (a minor changeset),
     `>=0.63.0` is the exact version floor.
   The consumer's spec says to bump the dependency to that version (`npm install
   @protolabsai/ui@^0.63.0` or the repo's lockfile command) and names the lockfile in
   `files_to_modify`.
5. **Check each repo's `release_freeze`** in the board config. The auto-merge edge holds
   merges while a repo is mid-release:
   - protoAgent (`prepare-release.yml` → `prepare-release/vX.Y.Z` PR): leave it unset. The
     defaults catch it.
   - protoContent (changesets): it should be `release_freeze: false`, because the Version
     PR is open whenever any changeset is pending. If it is unset there, note that to the
     operator and don't change config yourself.
6. **Mark both ready.** The consumer sits in `ready` showing `waiting on publish`, and the
   loop claims it once the version is on npm. `board_check_gates <id>` checks now.
7. **Optional, once the release PR exists:** add `pr:<owner>/<repo>#<n>` with
   `board_update_feature(feature_id, waits_for="npm:…, pr:…")`. `waits_for` REPLACES the
   list, so repeat the npm spec. This makes the step visible. The npm gate is still the
   one that decides.

## Worked example

The Design System agent asks for a `--pl-color-accent-subtle` token and a Badge
`tone="subtle"`, adopted in protoAgent's console. `npm view @protolabsai/ui version` →
`0.62.0`
(or `board_check_gates` on the consumer card reads `(latest 0.62.0)`).

```
board_create_feature(
  project="protoContent",
  title="Add accent-subtle token and Badge subtle tone",
  spec="Add --pl-color-accent-subtle to packages/design; add tone='subtle' to Badge in
        packages/ui. Ship .changeset/accent-subtle.md: minor for @protolabsai/design and
        @protolabsai/ui.",
  acceptance_criteria="- WHEN a Badge renders with tone='subtle' THE SYSTEM SHALL …",
  files_to_modify="packages/design/src/tokens.ts, packages/ui/src/Badge.tsx, .changeset/accent-subtle.md (new)",
)                                                    → bd-a1

board_create_feature(
  project="protoAgent",
  title="Adopt the Badge subtle tone in the console",
  spec="Bump @protolabsai/ui in apps/web to the release carrying tone='subtle' (>0.62.0)
        and use it for the idle status badge.",
  acceptance_criteria="- WHEN an agent is idle THE SYSTEM SHALL render its status Badge with tone='subtle' …",
  files_to_modify="apps/web/package.json, package-lock.json, apps/web/src/components/StatusBadge.tsx",
  depends_on="bd-a1",
  waits_for="npm:@protolabsai/ui@>0.62.0",
)                                                    → bd-c2

board_mark_ready("bd-a1"); board_mark_ready("bd-c2")
```

What you will see on bd-c2:

- `waiting on publish: npm @protolabsai/ui >0.62.0 (latest 0.62.0)` after bd-a1 merges
  and while the release PR is open or its publish job runs;
- claimed with `publish gates cleared (npm @protolabsai/ui >0.62.0 (0.63.0 published))`
  once 0.63.0 is on npm;
- possibly `held: release freeze (PR #… (prepare-release/v…))` on its own PR if protoAgent
  is cutting a release when it goes green. It merges by itself when the release lands.

## Rules

- Never gate a consumer on `depends_on` alone when it installs what the producer
  publishes.
- A publish gate never replaces the consumer's own acceptance criteria. The card still
  has to prove it uses the new API.
- `(check failed: …)` on a card means the registry or GitHub could not be read. The card
  stays held (fail closed). Report it if it persists and don't remove the gate to get
  past it.
- A spec that does not parse is refused at create/update, and the error names the part
  that is wrong. Fix the spec rather than dropping it.
