- **First-use `br init` no longer adopts `~/.beads`, and an older `br` on PATH no longer
  creates a fresh board store.** The instance store was bootstrapped with a bare
  `br init`, whose discovery walks up from the store dir; under a home with its own
  `~/.beads`, br refused ("ordinary commands never migrate…") and every board read failed.
  The init now names its `--db`. Separately, when the `br` on PATH is older than the pinned
  release and the store does not exist yet, the board fetches its pinned br instead of
  letting the older one create a db the pin would later refuse (an existing store keeps
  the br that made it; a failed fetch falls back to the PATH br).
- **The coder-monitor drawer reads cleanly.** "Saying" starts a new paragraph after a tool
  call or plan update instead of gluing sentences ("…the feature.Let me check…"), and drops
  claude-agent-acp's whole-block replay (mirrors protoAgent #3408/#3979). Paths show
  relative to the card's worktree (or its repo), else the basename, and never the home
  directory. The current tool shows what it does in plain words (its description, command
  or pattern) instead of raw JSON args; the tool feed lists each call once on one line with
  no raw `[edit]` kinds; "saying" opens scrolled to the newest line; the board header shows
  only the br version (the path is in its tooltip).
- **The coder's plan can reach the drawer.** The coder prompt now asks for a short checklist
  in the session's to-do/task tool (Claude Code's SDK sessions expose `TaskCreate`/
  `TaskUpdate`, not `TodoWrite`; claude-agent-acp turns either into ACP `plan` updates).
  The plan streams live on a host whose `dispatch_tapped` seam takes `on_plan`; on older
  hosts it still lands when the turn ends.
