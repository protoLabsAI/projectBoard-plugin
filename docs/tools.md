# Agent tools reference

The tools the board exposes to its own agent. They are ordinary LangChain tools, so any
agent on the instance can call them — which is the point: the PM agent files and shapes
work by talking, not by editing YAML.

`tests/test_docs_reference.py` fails if a tool is added or renamed without this file
changing.

⚠️ marks a tool that **changes a card's lifecycle state**. They are safe (every one is
auditable on the bead and reversible) but they are not read-only, and an agent given
these can retire work.

| Tool | Arguments | What it does |
|---|---|---|
| `board_block_feature` ⚠️ | `feature_id, reason` | Flag a feature `blocked` with a `reason` — it stays ON the board (blocked is a flag, not a lane) with the reason visible, and is skipped by the puller until cleared. Complements the board_update_feature repair path: block when the feature is stuck on something external (a missing dep, an unanswered question) and you wa… |
| `board_cancel_feature` ⚠️ | `feature_id, reason` | Cancel a feature created in error (bad decomposition, duplicate, scope cut) — the verb that RETIRES a bad card. Tags the bead `cancelled` and closes it with an auditable `reason` (`br close -r`), a second terminal edge that keeps the card (and its history) visible in a distinct `cancelled` state — NOT `done`, so the me… |
| `board_create_epic` | `title, description` | Create a top-level epic (a container for milestones/features). |
| `board_create_feature` | `title, spec, acceptance_criteria, files_to_modify, design, parent, priority, difficulty, depends_on, foundation, force, source_issue, project` | Create a board feature (a bead; starts in `backlog`). To pass the Ready gate a feature needs a self-sufficient `spec`, testable `acceptance_criteria`, AND `files_to_modify` (comma-separated paths to create/modify — vague tasks make a coding agent produce nothing). `parent` is the epic/milestone id; `difficulty` (small|… |
| `board_create_task` | `title, spec, acceptance_criteria, assignee, priority, parent, depends_on, project, source_issue, force` | Create a board TASK (a `task`-type bead; starts in `backlog`) — the sibling of board_create_feature for work that ships a DELIVERABLE (a doc, a decision, an artifact ref) instead of a PR (#217). A task rides the SAME rails as a coding feature (ready → claim → in_progress → in_review) but takes the delivery/verify edges… |
| `board_deliver` | `feature_id, text, ref` | Record a TASK's deliverable (#217) — the task sibling of a coder's open_pr edge, moving the bead in_progress → in_review with NO PR. `text` is the deliverable itself (recorded as a `deliverable:` comment the board reads back into the `deliverable` field); optional `ref` (a doc URL, an artifact path) lands on the same `… |
| `board_get_feature` | `feature_id` | Read a single feature's FULL detail as JSON — `title`, `spec`, `acceptance_criteria`, `design`, `state`, `labels`, `pr_url`, `difficulty`, `files_to_modify`, `foundation`, `priority`, `source_issue`, `project` (the entry in the board's `projects:` map this feature builds in, #90), `requirements` (the tracked requiremen… |
| `board_list` | `state, include_archived, with_ci, failing_only, project` | List board features, optionally filtered by board `state` (backlog/ready/ in_progress/in_review/done/blocked). Priority order. Terminal features (done/cancelled) past the archive window carry an `archived` label and are EXCLUDED from this default response (#115) — the live board, not all of history. Pass `include_archi… |
| `board_mark_done` ⚠️ | `feature_id, reason` | Mark a feature `done` by hand — the MANUAL Done edge (#228), for work that shipped OUTSIDE the board's PR lifecycle (the change landed via another repo/tool, or the feature completed off-board), where record_merge's automatic pr_url match never fires. Accepts only an in-flight card (in_progress / in_review / blocked) a… |
| `board_mark_ready` | `feature_id` | Promote a feature backlog → ready. Fails if it lacks a spec + acceptance_criteria (the Ready gate). Only `ready` features are pulled. |
| `board_requeue_ci_fix` ⚠️ | `feature_id, ci_failure` | Requeue a bounced coding feature for a CI-fix round on its existing PR. Lifecycle: `open_review` enters `in_review` with `pr_url`; `bounce_ci_fail` intentionally removes `in-review` and parks the same open-PR feature in `in_progress`; this verb records the concrete CI failure as a distinct `CI fix requested:` comment, queues it into the next coder prompt, and requeues the same card to `ready` with its PR preserved. Accepts only a coding feature in `in_progress` with non-empty `ci_failure` and an open `pr_url`; use `board_requeue_feature(feature_id, findings=...)` for adverse human review, which still requires `in_review`. |
| `board_requeue_feature` ⚠️ | `feature_id, findings` | Put a feature back to `ready` for re-dispatch, keeping its open PR — the verb the fix-round doctrine needs to carry review findings to the SAME branch. WITH `findings` (an adverse review's requested changes): mirrors `POST /features/{fid}/review` — records a DISTINCT review-bounce comment on the bead (`record_review_bo… |
| `board_reset_merged_verify_budget` | `feature_id` | Reset ONE feature's merged-state re-verify budget (ADR 0326, #326) — the supported way to un-stick an in_review card whose `next_action` reads `auto-merge held: merged-verify budget exhausted`. When a sibling merge keeps moving base under an in_review PR, the loop re-runs the gate against the merged state and stamps `m… |
| `board_retro` | `—` | Retro the board: mine the attempt/outcome history of completed + blocked features into recurring failure CLASSES + flow stats (escalation / block / multi-attempt rates + the blocked features and why). The loop-retro skill reads this to distill durable grounding (PROTO.md gotchas) so the next runs stop repeating known f… |
| `board_unblock_feature` | `feature_id` | Clear the `blocked` flag so the feature can be re-dispatched — the inverse of board_block_feature. Removes the blocked label; the puller can claim it again once it's otherwise `ready`. |
| `board_update_feature` | `feature_id, title, spec, acceptance_criteria, files_to_modify, design, difficulty, depends_on, foundation, source_issue` | Partially update an existing feature — the REPAIR path for a bead the Ready gate rejects. Only the non-empty arguments are written; every other field is left as-is. Use it to fill a missing `spec`, `acceptance_criteria`, or `files_to_modify` (comma-separated paths) on a feature `board_mark_ready` refused, then mark it… |
| `board_verify` | `feature_id, approved, feedback, by` | Verify a TASK's delivered work (#217) — the task Done edge, record_merge's verify sibling. `approved=true` CLOSES the task `done` with an auditable `verified: <by>` reason (a task has no PR to merge, so a verifier's approval is what closes it). `approved=false` records `feedback` as a comment (the next dispatch prompt… |

## Notes

- Every tool returns **JSON on success** and a plain `Error: …` string on failure. They
  never raise into the agent loop — a bad id is a message, not a traceback.
- `feature_id` is a bead id (`bd-a1b2`).
- Tools that accept prose (`spec`, `reason`, `findings`, `ci_failure`) strip literal
  wrapping quotes first, because models frequently emit `"…"` around a whole argument.
- The board must be **set up** for these to work: `br` on PATH, a `repo`, and — for the
  dispatch verbs — a coder delegate. `GET /status` reports what is missing.
