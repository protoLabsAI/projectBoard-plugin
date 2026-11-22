The last slice of a split issue now closes that issue when its PR merges (#834).

- **A busy board no longer turns `Fixes` into `Refs`.** Before stamping `Fixes #N` or
  `Refs #N`, the board checks whether another slice of the same issue is still being coded.
  If the board's store failed that read once (for example under heavy `br` load), the slice
  quietly got `Refs #N`, and the issue stayed open after the last slice merged. The read is
  now tried three times with a short pause between attempts. If every attempt fails, the
  board still plays safe and stamps `Refs`, but it logs a warning naming the card, the issue
  and the reason.
- **Each `Refs` decision is logged with its reason:** the id of the sibling slice that is
  still coding, a store that could not be read, or a requirements ledger where every item
  was declined.
- **The title keeps its closing `#N` when the source issue is written as `owner/repo#N`.**
  Before, the board removed the `#` from a title like `… (Fixes #5400, last slice)` whenever
  the card's source issue named its repo, even when that was the PR's own repo. The board
  now checks the repo the PR targets, so a sole or last slice keeps `#N` in its title.
  Fix rounds that rewrite the PR description keep the same `Fixes #N` line.
