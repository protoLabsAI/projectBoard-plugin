- **The board loop's failures now reach the host agent's friction ledger.** Coder drives hanging on dropped model connections, an epic base missing locally ("no commits vs base"), worktrees vanishing under a drive and preflight holds all happen in the background loop, outside any agent turn. So protoEngineer's friction review stayed empty through epic #4076. On a core with `graph.sdk.record_friction` (protoAgent#4164) and the friction plugin on, the loop records each as `harness` friction with source `plugin:project_board`:
  - a failed drive, by kind: timed out or hung, lost its ACP or model connection, returned no result, no commits vs base, worktree gone, stranded work, unwinnable oracle;
  - a drive that crashed unexpectedly;
  - a terminal block the loop sets;
  - a red gate preflight, once per distinct failure;
  - a merged-state verify that keeps failing to get a dependency tree;
  - a failed PR reconcile;
  - a crashed loop phase or crash recovery.

  Summaries carry no ids, so repeats group, and the card, branch and output go in the detail. Each failure is recorded at most once an hour per card, and at most 60 entries an hour go out in total. An older core, or one with the friction plugin off, records nothing and changes nothing.
