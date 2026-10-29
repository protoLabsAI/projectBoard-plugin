An **ACP-driven task now runs in a real project checkout** (#654, part 1 of the ACP-task
epic). Until now `coder_seam.dispatch_task` dispatched a task to its ACP assignee with no
working directory and `_drive_task` cut no tree, so a task that reached a coder had nowhere
to run.

- `_drive_task` now cuts a **scratch workdir** before dispatch: a worktree of the card's
  project at its base branch — cut with the same `create_worktree` naming a coding drive
  uses, so `feature_worktrees` / `reap_feature_worktree` / the health sweep all find it —
  or, when the card has no resolvable project repo, a bare `tempfile.mkdtemp`. The project's
  `setup_cmd` installs into the worktree case (best-effort).
- `dispatch_task` gained a `cwd` parameter: given a `cwd` and an ACP assignee, it scopes the
  session to that directory exactly as `dispatch_coder` scopes a coder to a worktree
  (per-dispatch copy with `cwd` as the workdir, board-owned git, sanitized env, a fresh
  session, and teardown of the scoped client). A2A assignees ignore `cwd`, and a call with
  no `cwd` is byte-identical to before.
- The scratch tree **never commits, pushes, or opens a PR** — the deliverable stays the
  reply → `record_delivery` → in_review. A worktree is reaped on the task's terminal edges
  (cancel, and now the `board_verify` approve Done edge); a requeue cuts a fresh tree on the
  next dispatch; a temp dir is removed when the drive ends.

Routing/classification is unchanged — this is only the workdir plumbing for tasks that
already reach an ACP assignee.
