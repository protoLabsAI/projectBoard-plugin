# Configure it in the console

On protoAgent 0.153.2+, **Settings → Plugins → Project Board → Configure** is split
by operator intent:

- **Projects** is the live registry editor. It lists the configured project map and
  can add or update repos only inside the host's enabled Project onboarding root.
  Add/update preserves sibling projects and file-only per-project fields without
  returning those fields' values to the browser, then reads live config back before
  reporting success. Malformed non-mapping entries stay visible but read-only until
  repaired in YAML (the API refuses to overwrite them too). A sole project is the
  runtime's implicit default; adding a second preserves that choice explicitly, and
  the sole default cannot be misleadingly “cleared.” Delete requires explicit
  confirmation and is refused while an active board card names that project—or while
  a legacy unlabelled active card routes through it as the effective default.
- **Automation** contains the loop posture, coder, concurrency/back-pressure, local
  verification, and candidate-generation controls.
- **Review & merge** contains review dispatch/gating, CI reconciliation, rebase,
  dependency release, and auto-merge behavior.
- **Security** contains the webhook HMAC secret. It is stored through the host's
  secret settings path, never returned by the Projects API.

Every scalar is explicit about apply behavior. `coder`, `br_autofetch`,
`max_concurrent`, `max_concurrent_sessions`, `max_pending_reviews`, and `auto_merge`
apply to the running loop; fields marked **restart** are persisted immediately but
do not change the already-constructed loop/router until the member restarts. Project
map/default changes apply live as one validated routing policy and do not produce a
false restart warning.

A save that sets a new local gate command, or moves the project (and its gate) to another
repo, runs that gate once on the clean base before anything persists, and a red gate refuses
the save. That save answers only after the gate has run, which takes minutes for a full test
suite. Saves to other projects don't wait on it. A save that leaves the gate and repo alone
doesn't re-run the gate. That includes a base-branch-only edit: the operator's checkout is
still on the old branch, so a smoke there could give no verdict. Instead, a registry change
resets that project's gate preflight in the loop. The preflight re-smokes the gate against
the new routing before any of the project's work dispatches, and it re-checks cards the
preflight is holding too. If another save changes the same project while a gate runs, the
save is refused with a 409; save again. When a proxy gives up on a long save first (the
fleet proxy allows 20s), the editor reads the outcome from `GET /projects`.

## Routing panel (board page)

The board page carries a third tab, **Routing**, next to Kanban and List. It is the
console surface for strict coder routing (`routing.exclusive`) — the operator can set
"Codex only" with no YAML.

The panel reads `GET /routing/effective` (a board read, same operator gate as the rest of
the page) and shows, per project:

- **Lifecycle paths** — `implement`, `fix (CI/review)`, and `escalate` are the coder paths
  the exclusive set governs; `review` (the review gate reviewer) and `helpers` (review
  dispatch, decompose, coder.solve offload, Max-Mode judge, task-assignee delegates) are
  marked **not covered by exclusive**, because strict routing is advisory for them.
- **The ladder** — one row per capability rung (standard / reasoning / advanced) with the
  delegates left after the exclusive filter, which sibling the next dispatch **opens on**,
  and **why** (round-robin, down-marked, `headroom ≥N%`, or exclusive). That reason is
  computed server-side with the loop's own sibling-preference functions, so what you read
  can't drift from what dispatch does.
- **Delegates** — each delegate's live provider state (live / down-marked, #420) and its
  subscription headroom (#579). An older host, or a delegate with no usage snapshot, reads
  **unknown** — never an error.

The control above the tables sets the board-scope exclusive set: tick the configured
delegates, use **Codex only** for the one-click headline case, **Apply selection** for any
other set, or **Clear (restore full ladder)** to drop the restriction. Every save goes
through B3's guarded, audited `PUT /routing`, so validation and the JSONL audit record
apply and the change is live on the running board immediately. A refused save — a
non-operator session, or a delegate that isn't configured/enabled — is surfaced inline and
changes nothing; the tables re-read afterward so they always reflect what actually landed.
Per-project `routing.exclusive` overrides remain YAML/`board_routing_set`-only and are shown
in the table with their source.

## Not exposed in the console

The console intentionally does not expose every manifest default. Structural legacy
single-repo bindings (`project`, `repo`, `base_branch`, `worktrees_root`, `db_path`)
remain file-only; use the Projects editor for multi-repo routing. Low-level timing and
retry budgets (`review_run_max`, `goal_fix_max`, `local_gate_max`,
`local_gate_timeout_s`, `coder_solve_test_timeout_s`, `coder_solve_budget`,
`coder_solve_k`, `coder_solve_tree_depth`, `loop_interval_s`, `coder_timeout_s`,
`merge_poll_interval_s`, `auto_merge_max`, `merged_verify_max`,
`health_sweep_interval_s`) also remain YAML-only expert tuning. Nested maps such as
`coders`, plus per-project advanced fields not owned by the editor, are preserved but
not flattened into lossy text inputs.
