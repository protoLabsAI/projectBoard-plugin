- **A pre-PR gate over a missing worktree no longer counts as a pass (#461).** When the tree
  had been reaped, the gate failed to launch with `FileNotFoundError`, and that was "treated
  as pass", so the drive went on to open a PR from a deleted directory. A gate whose tree is
  gone now fails the drive, before it launches or by the time it ends, with
  `worktree missing: <path>` (class `transient`, so the sweep requeues it). It opens and
  updates no PR. A keep-worktree re-dispatch checks its tree before it starts a coder. A gate
  that times out on a healthy tree still passes, since CI still gates.
- **The health sweep asks everything that can hold a tree before it reaps one (#461).** It
  checks the live-drive registry, the card's running reconcile, merge gate and review gate,
  and any live process whose cwd is inside the tree. A drive still running for a card that
  has closed is cancelled first, and its tree is reaped on a later sweep.
