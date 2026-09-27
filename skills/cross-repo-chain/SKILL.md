---
name: cross-repo-chain
description: >-
  Use when one piece of work has to land in one repo, be RELEASED or PUBLISHED (npm
  package, GitHub release, a release PR merged), and only then be picked up in another
  repo. The usual case is a design-system change in protoContent (published as
  @protolabsai/ui / @protolabsai/design) that protoAgent's console then adopts. Writes the
  chain as board cards whose consumer waits on a publish PROVEN to contain the change
  (`waits_for: npm:<pkg>@contains:<repo>@<card-id>`), not on a merge (`depends_on`) or a
  version guess (`>0.62.0`). Plans cards, does not write code.
tools:
  - board_list          # what is already on the board (don't duplicate a card)
  - board_create_feature
  - board_create_task
  - board_update_feature  # replace a card's waits_for later
  - board_get_feature   # read a card's gates and next_action back
  - board_check_gates   # check the gates now instead of at the loop's next sweep
  - board_mark_ready
  - read_file           # the producing repo's .github/workflows + .changeset/ (its release mechanism)
  - list_dir
---

# Cross-repo chains: change → publish → adopt

`depends_on` releases a card when its blocker **merges**, which is too early for a
consumer that installs what the blocker publishes. A **version floor** is wrong too. In a
changesets repo the "chore: release packages" PR may already be open when you write the
cards, and it can publish `0.62.1` **without** the change before the change merges. Then
`>0.62.0` (or `>=0.63.0`) is met by the wrong publish.

Gate the consumer on the change itself:

```
waits_for="npm:@protolabsai/ui@contains:protoLabsAI/protoContent@<producer card id>"
```

This holds only when the newest published `@protolabsai/ui` is git-tagged at the
producer card's merge commit or a descendant of it. It stays unmet until the producer
card's PR has merged, then until a publish carrying it lands. No version to guess, and
`depends_on` isn't needed for correctness (add it anyway if you want the order shown on
the board). The mechanics are in `docs/publish-gates.md`.

Other gate kinds, when they fit:

```
waits_for="release:protoLabsAI/protoCLI@>=1.4.0"                        # plain-tag repo: a GitHub release >= 1.4.0
waits_for="release:protoLabsAI/protoContent@@protolabsai/design@>=0.9.3" # per-package tags need the package
waits_for="pr:protoLabsAI/protoContent#219"                             # that PR merged
```

A bare `release:<repo>@<range>` on a repo that tags per package (changesets) is
**refused**, because any package's version would satisfy it. Name the package.

## Steps

1. **Find the producing repo's release mechanism.** Read its `.github/workflows/`.
   - **changesets** (protoContent): the change needs a `.changeset/*.md`, or nothing is
     ever published. The action publishes `<package>@<version>` tags, which is what
     `contains:` reads.
   - **plain `v*` tags / releases**: `contains:` works too, since it falls back to the
     `v<version>` tag. Otherwise gate on `release:`.
2. **Write the change card** in the producing project. Include the changeset in
   `files_to_modify` (`.changeset/<slug>.md (new)`), and say in the spec which packages
   bump and at what level.
3. **Write the consumer card** in the consuming project with
   `waits_for="npm:<package>@contains:<owner>/<repo>@<change card id>"`. One gate per
   package the consumer needs from that change. Its spec says to bump the dependency to
   the release that carries the change and names the lockfile in `files_to_modify`.
4. **Check each repo's `release_freeze`** in the board config. The auto-merge edge holds
   merges while a repo is mid-release:
   - protoAgent (`prepare-release.yml` → `prepare-release/vX.Y.Z` PR → tag): leave it
     unset, the defaults catch it.
   - protoContent (changesets): it should be `release_freeze: false`. If it isn't, tell
     the operator; don't change config yourself.
5. **Mark both ready.** The consumer sits in `ready` reading
   `waiting on publish: npm @protolabsai/ui containing protoLabsAI/protoContent@bd-a1 (…)`.
   The parenthesis says which step it is at: `not merged yet`, then
   `latest 0.62.1 … lacks …`, then it is claimed. `board_check_gates <id>` checks now.

## Worked example

The Design System agent asks for a `--pl-color-accent-subtle` token and a Badge
`tone="subtle"`, adopted in protoAgent's console.

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
  spec="Bump @protolabsai/ui in apps/web to the release that carries tone='subtle'
        (the version board_get_feature's gate detail names) and use it for the idle
        status badge.",
  acceptance_criteria="- WHEN an agent is idle THE SYSTEM SHALL render its status Badge with tone='subtle' …",
  files_to_modify="apps/web/package.json, package-lock.json, apps/web/src/components/StatusBadge.tsx",
  waits_for="npm:@protolabsai/ui@contains:protoLabsAI/protoContent@bd-a1",
)                                                    → bd-c2

board_mark_ready("bd-a1"); board_mark_ready("bd-c2")
```

What you will see on bd-c2:

- `(card bd-a1 not merged yet (#…))` while bd-a1 is in review. This is true even if the
  already-open Version PR publishes a patch meanwhile.
- `(latest 0.62.1, tag … lacks …)` after bd-a1 merges, while the regenerated Version PR is
  open or its publish job runs;
- claimed, with `publish gates cleared (… (0.63.0 published, contains …))`;
- possibly `held: release freeze (…)` on its own PR if protoAgent is cutting a release when
  it goes green. It merges by itself once the release is tagged.

## Rules

- Never gate a consumer on `depends_on` alone, or on a version floor you picked, when it
  installs what the producer publishes. Use `contains:`.
- A publish gate never replaces the consumer's own acceptance criteria. The card still
  has to prove it uses the new API.
- `(check failed: …)` means the registry or GitHub could not be read. The card stays held
  (fail closed). Report it if it persists; don't remove the gate to get past it.
- `(… has no tag … to prove it)` means the producer published without a git tag. Tell the
  operator; don't swap in a version floor.
- A spec the board refuses comes with the reason and the syntax it wants. Fix the spec,
  don't drop it.
