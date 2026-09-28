# HTTP API reference

Every route below is real and generated from `api.py`; `tests/test_docs_reference.py`
fails if a route is added, removed or renamed without this file changing. If something
here looks wrong, the test is the thing to trust — file a bug.

There are **two surfaces**, with different authentication, and the split is deliberate.

## Operator surface — bearer token

Mounted under `/api/plugins/project_board`. These are the routes the console and the
board's own tools use. They require the instance's operator bearer token:

```bash
TOKEN=$(cat "$HOME/Library/Application Support/studio.protolabs.protoagent/workspaces/.fleet-token")
curl -H "Authorization: Bearer $TOKEN" \
     http://127.0.0.1:7870/api/plugins/project_board/features
```

| Method | Path | What it does |
|---|---|---|
| `GET` | `/projects` | Live project config for the custom Configure tab (no router snapshot). |
| `PUT` | `/projects/{name}` | Add/update one boarded repo through the same bounded seam as the agent tool. A save that sets a new gate command, or moves the project to another repo, answers only after that gate has run once on the clean base (minutes for a full suite); a red gate is a 400 naming the failure, a 409 means another save changed the project meanwhile. See [Long saves](#long-saves). The same save registers the repo as a read-only managed project (#452); the response's `managed_project` is `{action, name, detail}`, where `action` is one of `added`, `updated`, `unchanged`, `present`, `skipped` or `unsupported`. |
| `DELETE` | `/projects/{name}` | Delete a project only after proving no active board card references it. Its managed project is removed only if the board added it, it still points at the board's repo, it is still read-only, and no other board project uses that checkout (ownership passes to that project otherwise); `managed_project.action` is `removed`, `kept` or `none`. |
| `GET` | `/status` | Is this board BOUND yet? A pure config read — no `br` calls — so it answers even when the store can't, which is exactly when the view needs it. The shipped default (`repo: "."`, no db_path, no project… It also serves what the loop's health sweep found: `base_checkouts` (each project's main checkout against `origin/<base>`: `current`, `fast_forwarded`, `stale`, `unknown`, or `skipped` when it isn't board-owned, #452), `stale_base_checkouts` (`{project: why}` for the ones it could not move), and `orphaned_cards` (live cards whose project is not in the map, each with its re-home call, #454). `setup.legacy_binding_hint` flags a flat `repo` left set beside `projects:`. |
| `POST` | `/epics` | — |
| `POST` | `/milestones` | — |
| `GET` | `/features` | The board listing (`?state=`, `?project=`, `?include_archived=`). A task row carries a small delivery signal — `delivered` (the current round: in review or done), `deliverable_chars`, `delivered_by`, a ≤280-char `deliverable_preview`, and `last_deliverable_preview` for a task sent back from review — never the full deliverable (#399). |
| `GET` | `/features/{fid}` | One card's full projection, including a task's whole `deliverable`, its `requirements` ledger, its `waits_for` publish gates with their last-checked `gates` verdicts, and `next_action` when something other than a coder moves it. |
| `GET` | `/features/{fid}/ready-check` | What the Ready gate would say about this card now, changing nothing (#455) — the REST twin of `board_check_ready`. Returns `{id, state, ok, refusals, advisories, breadth, unserialised, suggested_edges}`: every failed check at once, each `{gate, message, fix}`; the breadth count after `breadth_exclude`; every pair of open cards sharing a file with no `depends_on` path between them, and the chain edges that would serialise them (#458). 404 for an unknown card. |
| `POST` | `/features/{fid}/gates/check` | Re-check this card's publish gates (`waits_for`) against npm / GitHub now — the REST twin of `board_check_gates`. Returns `{id, waits_for, gates, clear}`; a failed check reads unmet with its `error` (fail closed). |
| `GET` | `/features/{fid}/progress` | Live coder-monitoring snapshot (#84) for the board view's monitor drawer. |
| `PATCH` | `/features/{fid}` | In-place spec edit — the REST complement of `board_update_feature`. Accepts `title`, `spec`, `acceptance_criteria`, `design`, `files_to_modify`, `difficulty`, `priority`, `source_issue`, `waits_for` (replaces the publish gates; `[]` clears); only non-null fields are… `project` re-homes the card (#454), with the same rules as `board_update_feature`. |
| `POST` | `/features` | Create a feature — the body is splatted into `store.create_feature`, so it accepts every create field, including `project` (#90): the entry in the board's `projects:` map the feature builds in, stampe… |
| `POST` | `/features/batch` | Batch-create a whole decomposition (#92). Body: `{"plan": [{title, spec, acceptance_criteria, files, difficulty, depends_on, foundation, source_issue}, …], "mark_ready": false}`. All-or-report: a malf… |
| `POST` | `/features/{fid}/dep` | Add a `blocks` edge: `fid` waits for `depends_on` to be merged→done. (Foundation gating is just a blocks-edge on the foundation feature.) |
| `DELETE` | `/features/{fid}/dep` | Remove a `blocks` edge — inverse of POST …/dep. Body: `{"depends_on": "<id>"}`. |
| `POST` | `/features/{fid}/ready` | The Ready gate (invariant #1) — 400 naming EVERY failed check at once, each with its fix (#455): required fields, phantom paths, breadth, design, shared files. `GET …/ready-check` asks the same question without promoting. |
| `POST` | `/features/{fid}/block` | Block a card by hand. Body: `{"reason": "…"}` (required). A hand-set block is a hold, never a failure: it is never cleared automatically, whatever the reason says (#406). Lift it with `POST …/unblock`. |
| `POST` | `/features/{fid}/unblock` | — |
| `POST` | `/features/{fid}/cancel` | Cancel a feature created in error — the second terminal edge (#47). Closes the bead with an audit reason and tags it `cancelled` (a distinct state, not `done`), so a bad decomposition/duplicate leaves… |
| `POST` | `/features/{fid}/done` | Mark a feature `done` by hand — the MANUAL Done edge (#228), for work that shipped OUTSIDE the board's PR lifecycle (record_merge's pr_url→external_ref match never fires). Accepts only an in-flight ca… |
| `POST` | `/features/{fid}/review` | The operator's adverse-review bounce (#473), the bearer-gated twin of the signed route of the same path below, so an operator can send a card back to a fix round with findings without the webhook secret. Body: `{findings: str, escalate?: bool=false}`. Records the `findings` as a distinct `review requested changes:` comment, leads the next dispatch prompt with them, and requeues onto the SAME open PR. `escalate: true` climbs the model ladder (Blocked at its top). 400 unless the card is `in_review`, and while the loop is still working it, a live drive or review gate (#398). Returns `{requeued, escalated, next_tier?, exhausted?, feature}`. |
| `POST` | `/features/{fid}/attach-pr` | Attach an externally opened PR to the existing coding card it belongs to (#402). The card moves to in_review with that PR, and the normal reconcile drives it from there (CI, the review gate, merge → done). Body: `{pr_url, reason?, by?}`. Returns 400… |
| `POST` | `/features/{fid}/salvage` | Publish a stranded card's worktree WITHOUT dispatching a coder (#427): commit what the tree holds, run the pre-PR gate, push, open the PR (or push onto the card's existing one), move the card to `in_review`. Body: `{force?: bool, tree?: str}`. An operator override: skips the goal, requirement-ledger and source-issue checks; CI and the review gate still apply. Only for a stranded card (`in_progress` with no live drive, or `blocked`); refuses (409, nothing changed) while a drive owns it, or when no worktree — or more than one, unless `tree` names one — has changes vs base. A red gate publishes nothing (409 `gate-red`, with the output's tail) unless `force: true`, which publishes it as a DRAFT carrying that output (an existing PR is converted, with the output posted on it); `draft` is read back from GitHub. 200 `published` · 404 `not-found` · 409 `cancelled` (a cancel landed mid-publish) · 503 `loop-not-running` · 502 `error` (`branch` names a branch already pushed). |
| `POST` | `/features/{fid}/deliver` | Record a task-type feature's DELIVERABLE (#217) — the task sibling of the coder's open_review edge, moving in_progress → in_review. Body: `{text?, ref?}`: `text` rides a `deliverable:` comment (the pr… |
| `POST` | `/features/{fid}/verify` | The task-type Done edge (#217) — `record_merge`'s verify sibling. Body: `{approved?: bool=true, feedback?, by?}`. `approved=true` closes the task with a `verified: <by>` reason; `approved=false` recor… |
| `DELETE` | `/features/{fid}` | Hard-delete a feature created in error — a `br` tombstone (the harder sibling of POST …/cancel). Goes through the board so board ↔ JSONL stay consistent; refuses (400) if the feature has dependents (d… |
| `POST` | `/features/{fid}/test-rung` | Run exactly ONE named rung of coder.solve() against this feature's REAL acceptance tests, in a throwaway worktree that's ALWAYS reaped — never promoted, no PR opened, no board state touched. For verif… |

## Public-of-necessity surface — HMAC signed

These cannot carry an operator bearer: GitHub signs its own webhooks, and an iframe
page-load cannot attach a header. They are mounted unauthenticated and every POST
crosses a fail-closed HMAC boundary (`X-Hub-Signature-256`) before touching the store.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/board` | — |
| `GET` | `/config/projects` | Public page chrome for the sandboxed Configure tab. |
| `POST` | `/features/{fid}/ci` | CI result for the feature's PR. `passed: true` is a no-op (merge sets done, via the webhook). `passed: false`: - with an escalation ladder → record + climb a tier and **requeue** to ready (the puller… 400 while the loop is still working the card, a live drive or review gate (#398). |
| `POST` | `/features/{fid}/review` | Adverse code-review bounce for the feature's open PR — the review sibling of `/ci` fail. Records the `findings` as a DISTINCT review-bounce comment on the bead (≠ ci-fail), feeds them into the next di… 400 while the loop is still working the card, a live drive or review gate (#398). |
| `POST` | `/webhook/pr` | GitHub PR webhook — the SINGLE Done edge. On a `closed` event with `merged: true` it sets the matching feature `done` (nothing else does) and reaps its worktree. The raw body is HMAC-verified against… |

## Long saves

`PUT /projects/{name}` runs the project's gate once on the clean base before it saves a new
gate command, or the same gate moved to another repo. The request answers only after that
gate has finished, which takes minutes for a full test suite. Nothing else waits on it: saves
to other projects proceed, and only the final read-merge-write is serialized. If another
save changes the same project while the gate runs, this one is refused with `409`; save
again.

A client that can't hold a request open that long can still learn the outcome. The fleet
proxy, for one, answers a plugin API call with `504` after 20s. Send a `request_id` in the
body, then read `GET /projects` → `saves.<name>`:

```json
{"id": "<your request_id>", "state": "running|saved|refused|cancelled",
 "stage": "running the gate on the clean base", "started_at": "…", "finished_at": "…",
 "detail": "<why it was refused>"}
```

The Projects editor does exactly this when the connection gives up.

## Conventions

- **`fid`** is a bead id (`bd-a1b2`), the board's primary key everywhere.
- Errors surface as `{"detail": "<reason>"}` with a 4xx; a `BoardError` from the store
  becomes a `400` with the store's own message, so the reason is the `br` failure itself.
- A **`503`** means the board store did not answer: a `br` call stalled past its timeout
  (45s) and was stopped (#404). Unlike a `400` this is not a refusal, and a write's outcome
  is unknown. It may have landed before the stall, so re-read the card before retrying.
- Writes are **idempotent where it matters** — re-recording a merge, re-flagging a block
  and re-stamping a verdict are all safe to retry.
- The board is a **projection over beads**. Every mutation shells `br`; nothing is cached
  behind it, so an external `br` edit is picked up on the next read.
