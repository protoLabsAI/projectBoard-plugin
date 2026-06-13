---
name: decompose-project
description: >-
  Use when asked to plan, decompose, or spec out a new project/idea before any code
  is written. Runs the adversarial decomposition pipeline (idea → outline → MADR
  decision records → epics › milestones › features with EARS acceptance criteria),
  writing the plan as docs in the working tree AND populating the beads board — with
  an antagonist hardening every step and a human approval gate per epic. The board's
  loop then builds the `ready` features. Do NOT use to write code; this only plans.
tools:
  - task            # drive the `decompose` (propose) + `antagonist` (attack) subagents
  - read_file
  - list_dir
  - find_files
  - write_file      # write planning docs to the tree (project must be read-write)
  - board_create_epic
  - board_create_feature
  - board_mark_ready
  - board_list
  - request_user_input   # the per-epic human approval gate
---

# Decompose a project (adversarial, docs-in-tree, board-backed)

You orchestrate a deterministic pipeline; the *thinking* is delegated to two
reasoning subagents and the *judgment to ship* to a human. The plan lives as
markdown in the repo (the source of truth) and as beads on the board (the execution
DAG). **Never write code here** — you produce specs and beads only.

## The tree you produce
```
docs/planning/00-idea.md  01-outline.md
docs/constitution.md                       # immutable principles (create once)
docs/decisions/NNNN-<slug>.md               # MADR ADRs (name rejected options)
specs/epic-NN-<slug>/epic.md
  milestone-NN-<slug>/
    feature-NNN-<slug>/requirements.md  design.md  tasks.md
```

## The per-step loop (use for EVERY artifact)
For each artifact you create, run **propose → attack → revise** before writing it:
1. `task(decompose, …)` — pass the **step**, the relevant context (point it at the
   idea + the docs already written), and (after round 1) the antagonist's feedback.
2. `task(antagonist, …)` — pass the same step + the proposed artifact. It returns
   `VERDICT: PASS` or `VERDICT: REVISE` + a numbered fix list.
3. If REVISE, go back to step 1 with the feedback. Cap at **3 rounds**; if it still
   won't pass, write it anyway with a `> TODO(plan): unresolved — <points>` note and
   flag it at the human gate.
4. On PASS, `write_file` the artifact to its path in the tree.

Run independent artifacts (e.g. several features in one milestone) via `task_batch`
to fan out, but keep the propose→attack pairing for each.

## Order of work — decompose epic-by-epic (the gate is per epic)
1. **Once per project:** ensure `docs/planning/00-idea.md` captures the raw idea
   (write it from the request if missing). Ensure `docs/constitution.md` exists —
   if not, run the loop to draft the project's immutable principles. Then run the
   loop for `01-outline.md`, then the load-bearing `docs/decisions/*.md` ADRs.
2. **Epics & milestones:** run the loop to produce the epic + milestone breakdown.
   Create the epic bead (`board_create_epic`). Mark a milestone `foundation: true`
   in its frontmatter **only** if the antagonist agreed it's genuinely shared
   structure (dependents will gate on its *merge*).
3. **For the FIRST epic only**, decompose its milestones into features: run the loop
   for each feature's `requirements.md` / `design.md` / `tasks.md`. For each feature
   call `board_create_feature(title, spec=<imperative requirements summary>,
   acceptance_criteria=<the EARS list>, files_to_modify="<comma-separated paths to
   create/modify>", design=<design summary>, parent=<epic/milestone id>, priority=…,
   difficulty=…, depends_on="<blocking feature ids>", foundation=<true|false>)`.
   **`files_to_modify` is required by the Ready gate** — name the exact paths, and
   write the spec imperatively (a vague task makes a coder produce nothing).
   Foundation edges are just `depends_on` on the foundation feature; set
   `foundation=True` on a shared-structure feature so dependents always gate on its
   **merge** (under `dep_gate: review`, non-foundation blockers release dependents at
   in_review — foundations never do).
4. **HUMAN GATE (per epic):** summarize the epic's features (titles, acceptance
   criteria, deps, which are foundations) and call `request_user_input` to ask the
   operator to approve, amend, or reject **before any feature goes `ready`**. This is
   the single highest-ROI checkpoint — do not skip it.
5. On approval, `board_mark_ready` each approved feature (the Ready gate re-checks
   spec + acceptance_criteria). The board loop now builds them.
6. **Do not decompose the next epic until this one is approved** (and ideally its
   first slice has built green) — tighter feedback, no over-planning. Repeat from 3.

## Rules
- The **docs are the source of truth**; the beads mirror them (link the feature
  bead to its `specs/.../feature-NNN/` dir in the spec body).
- Honor `docs/constitution.md` and cite ADRs in `design.md` — the antagonist checks this.
- A feature is `ready` only with a self-sufficient spec + EARS acceptance criteria a
  junior could pick up. If the antagonist couldn't get it there, it does NOT go ready.
- Keep `difficulty` optional — only set it if the operator runs a multi-tier coder
  ladder; otherwise omit it (it's ignored with a single coder).
