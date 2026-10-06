- **The merged-state verify no longer reads a broken dependency tree as "the RESULT is
  broken", and never terminal-blocks a card that merged while its gate ran.** Two designSystem
  cards (ds-xof / PR #567 and ds-h5s / PR #565), both docs-only, were blocked and paged a
  human. In the throwaway `.verify-feat-…` tree, pnpm reinstalled node_modules from scratch,
  then `tsc` died with `MODULE_NOT_FOUND` under `node_modules/`. The board filed it as a red
  gate, though their CI was green and one card was already done. Now a failed `setup_cmd` install skips the
  merged-state gate. Gate output that shows a broken tree (a `Cannot find module` under
  `node_modules/`, pnpm's reinstall prompt, `ERR_PNPM_*` other than `OUTDATED_LOCKFILE`) is
  no verdict. Both retry next poll without a stamp or budget. After three in a row the board
  posts one `INFRA:` warning and card comment, and records a no-verdict run. Before a real
  red blocks, the board re-reads the PR and the card. If the PR merged or the card is done,
  the red is reported in a warning and a card comment, and the card stays done. If the PR
  closed or the card left review, the block is skipped.
