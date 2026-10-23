# Using the board

Day-to-day operation: agent tools, skills, the HTTP surface and the console view.

- **Headless / via the agent:** `board_create_epic`, `board_create_feature`
  (`title`, `spec`, `acceptance_criteria`, `files_to_modify`, `depends_on`, …),
  `board_mark_ready`, `board_list`. Every `in_review` row of `board_list` (and of
  `GET …/features`) carries `next_action` — `awaiting-merge (auto_merge off)` /
  `auto-merge pending` / `review in progress` / `changes requested` / `awaiting
  review verdict (no review-clean)` / `merge-hold (operator veto)` / `blocked` /
  `draft (run gh pr ready)` — plus `awaiting_merge: true` and a `next_action_hint`
  ("auto_merge is off — merge #N or turn it on in Settings ▸ Project Board") for the
  first. Derived from the review sub-state labels + the board's LIVE
  `auto_merge`/`review_gate` config (the same decoding the loop's merge edge uses;
  `store.merge_posture`; a Settings save to `auto_merge` flips it with no restart),
  no network. `board_list(with_ci=true)` demotes a red row to `ci failing` — never
  "merge #N" on a red PR.
- **Onboard a repo:** the `onboard-project` skill, BEFORE decomposing or dispatching
  anything at a repo this board has not worked before. It checks the preconditions a
  coding-agent loop needs (a runnable gate, conventions, a reachable base branch), fixes
  the safe deterministic ones, and reports what a human still has to decide.
- **Plan a project:** the `decompose-project` skill ("decompose <idea>") runs the
  adversarial pipeline and populates the board.
- **Learn from the loop:** the `loop-retro` skill turns `board_retro`'s failure classes
  into written grounding, so a recurring failure becomes a rule instead of a habit.
- **A card from chat:** the `card-from-chat` skill. Ask for a feature and the agent
  answers with a small inline form (an ADR 0118 interactive answer: Project from
  `board_projects`, Title, What and why, Acceptance criteria, Priority). **Create card**
  proposes the filled values as your next message (the console's "Send to chat" confirm).
  The agent then creates the card, marks it ready and calls `board_watch`, which opens the
  Board on the main stage and follows the card lane by lane until it is done.
- **HTTP API:** operator reads and mutations live under the bearer-gated
  `/api/plugins/project_board/*` prefix. The public prefix exposes only the board
  iframe plus `/webhook/pr`, `/features/{id}/ci`, and `/features/{id}/review` for
  external systems. Every public POST requires
  `X-Hub-Signature-256: sha256=<HMAC-SHA256(raw-body, webhook_secret)>`; a blank
  secret disables public mutations with 503. GitHub signs `/webhook/pr` natively;
  CI/review callers must sign the exact JSON bytes they send. An operator bounces a
  card with findings WITHOUT the secret through the bearer-gated twin,
  `POST /api/plugins/project_board/features/{id}/review` (#473).
- **Watch it:** the **Board** console view (left-rail) at
  `/plugins/project_board/board` — Kanban + list, live-refreshing, served by the
  same router as the API (so the declared view path is genuinely mounted).
  Cards glide between lanes as the loop moves them (within ~2 s: the page polls the
  store's change stamp, `GET …/rev`, and re-reads only when it moves), and a building
  card shows who is coding, for how long and its last tool call. You can move a card
  yourself where the API has an operator action for it: drag a backlog card onto
  **on deck** (`POST …/ready` — a gate refusal rolls the card back with the reason),
  drag within backlog or on deck to re-rank it (`PATCH …` `priority`), or focus a card
  and press **M** for the same moves plus block / unblock (**Alt+→** marks ready,
  **Alt+↑/↓** re-ranks). Building, in review and done belong to the loop and are
  read-only; there is no un-ready route, so a card leaves on deck only when it is claimed.

### External evidence and early WIP publication

A coding dispatch records external paths from structured ACP tool locations and
`cwd`, `workdir`, `path`, or `file_path` inputs. It records file evidence and, within
approved filesystem roots, its enclosing git checkout. Shell commands, narration,
and final-answer prose do not authorize inspection or publication. Paths inside the
board's own project checkout or its dispatch worktree are never treated as external
work — that remains worktree salvage's job — so a coder reading the main checkout by
absolute path can never push the project's own HEAD.

Capture has two seams. During the coder's tool stream it only ACCUMULATES the
observed paths in memory — no git, no store, no network on that hot path, so it
cannot stall the stream or eat into the dispatch timeout. Once the dispatch ends —
a clean reply, a partial/error result, a timeout, or an operator cancel — the drive
drains once: it probes each path locally, persists the attributable identity, and
publishes early. The drain is shielded so a durable write finishes even while the
dispatch is being cancelled, and it is wholly best-effort: a store, filesystem, or
transport failure during capture is logged and swallowed, never allowed to turn a
healthy build into a failed drive.

For work outside an observable coding dispatch (including ordinary delegated task
replies and unrelated scratch jobs), the lead or delegate must call
`board_record_work_location` immediately with a stable id, path, purpose, branch,
SHA and honest pushed status. Do this before a hold or context compaction. Use
`pushed=false` or unknown until remote durability is established; never include
credentials. Reporting a path grants no filesystem or remote permissions.

For an observed external checkout, the board saves the local identity before any
push. With existing filesystem **write** authorization for both the checkout and
project, it attempts a normal push of that exact committed SHA to
`refs/heads/wip/<card-id>` on the project's existing `origin`. The external
checkout must have the same origin. Local bare remotes also require write
permission; HTTPS remotes must not contain credentials. Transport runs from the authorized
project checkout, using its existing HTTPS credential helpers and the external
checkout's authorized object store; helper values are never recorded on the card.
Unsupported transports,
remote rewrites, push URL overrides, and differing origins require explicit
operator publication. No staging, committing, force push, or push to main occurs.
A successful push is recorded as durable only after the advertised remote ref is
verified to contain the commit. Existing worktree salvage behavior is unchanged.

After a failure or restart, open the card and call `board_list_work_locations`.
The persisted entry contains the exact path, branch, SHA, session attribution and
`last_check.next_step`, including a shell-quoted recovery command. Preserve that
checkout and any uncommitted evidence. Resolve write approval, network/rate-limit
failure, or a conflicting WIP ref before retrying; never force over a conflict.
Verify origin against the project's authorized origin before running the suggested
command. A subsequent observed dispatch can retry publication from the recorded
path; recovery needs neither agent memory nor a search of temporary directories.
Explicitly reported evidence on another branch remains available through the same
record/recover tools, without granting automatic push authority. Card completion,
holds and cancellation do not remove these external locations.
