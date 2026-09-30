- **A red job whose workflow run is still going now waits for the run instead of spending a fix round (#487).**
  A fast job can fail while its sibling jobs are still running, and GitHub refuses to rerun a
  run until it finishes (`This workflow is already running`). The board read that refusal as
  "nothing to rerun" and bounced the card straight into a coder fix round. With nothing to fix
  in the diff, bd-4fsn spent three rounds (sonnet → opus → opus), each correctly committing
  nothing, and went terminal-blocked on an unrelated flaky test. The card now waits a pass,
  unstamped and with no fix round spent, logs the wait once, and reruns once the run completes.
  Any other refusal still bounces as before.
