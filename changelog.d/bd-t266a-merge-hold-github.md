A card's `merge-hold` now reaches GitHub. The PR reconcile puts the `merge-hold` label on a
held card's PR (creating it in the repo if missing) and disables GitHub auto-merge if it is
armed, so pr-reviewer's `gh pr merge --auto` can no longer merge a held card. Releasing the
hold removes the label only if the board added it (`gh-merge-hold` bead marker); a label a
human put on the PR is left alone. A failed GitHub write logs a warning and leaves one bead
comment; the board's own hold is unaffected.
