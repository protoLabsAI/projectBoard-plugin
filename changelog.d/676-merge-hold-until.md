A merge-hold can now carry a checkable release condition (#676). `board_hold_merge` takes an
optional `until` — comma-separated `pr:` / `release:` / `npm:` gate specs in the same grammar
as `waits_for` — stored on the card as `merge-hold-until:` notes lines; a bad spec is refused
with the parse error and the card is left untouched. `board_release_merge_hold` evaluates the
condition live and refuses while any spec is unmet (a check that errors counts as unmet),
naming each one; `force=true` overrides, and the audit comment records the override and the
unmet specs. Re-holding a held card with a new reason or `until` replaces both with one
`merge-hold updated:` audit comment. `board_get_feature` shows `merge_hold_until` (per-spec
cached verdicts, no network), and an in-review card held on an unmet condition reads
`merge-hold until <spec> (unmet)`. A hold without `until` behaves exactly as before, and a
hold is never released automatically.
