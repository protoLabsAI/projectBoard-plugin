---
name: onboard-project
description: >-
  Use FIRST, when pointed at a repo this team hasn't worked before (or to re-check a
  repo's readiness) — BEFORE decomposing or dispatching any feature. Scans the repo
  for the preconditions a coding-agent loop needs, AUTO-FIXES the safe/deterministic
  gaps (beads workspace, ignored agent scratch, the build/test gate) by delegating to
  the coder, and BOARDS the judgment gaps (a project grounding doc, PR CI) as
  features. Ends with a readiness report + a human gate. Do NOT use to plan features
  or write code — that's `decompose-project` and the coder.
tools:
  - read_file        # scan the repo (read-only)
  - list_dir
  - find_files
  - delegate_to      # hand the safe in-repo init (br init, gitignore) to the coder — you have no shell
  - board_create_feature
  - board_mark_ready
  - board_list
  - request_user_input   # the readiness human gate
  - write_file       # optional: write the readiness report into the tree
---

# Onboard a project (readiness before the loop)

The board's loop assumes a **prepared** repo. When it isn't, the failures aren't the
coder's fault — they're readiness gaps: the board writes to the wrong beads db, the
coder's scratch leaks into PRs, the gate is undeclared, or the coder fabricates a
convention it was never told. This skill prepares the repo so the team can be let
loose. **You orchestrate**: you scan, you *delegate* the in-repo hands-on work to the
coder (you have no shell of your own), and you *board* the work that needs judgment.

## What "ready" means — the checklist

| # | Item | Ready when |
|---|---|---|
| 1 | **Board** | a `.beads/` workspace exists in the repo (so the board pins here, not a parent dir) |
| 2 | **Hygiene** | `.gitignore` ignores the coding agent's **per-session scratch** (for proto: `.proto/memory/`, `.proto/session-notes.md`, `.proto/repo-map-cache.json` — **not** all of `.proto/`, whose `evolve/` holds versioned skills) **and** the build output dir |
| 3 | **Gate** | the repo's real build/test command is known and set as `project_board.local_gate_cmd` |
| 4 | **Grounding** | a context doc the coder reads: conventions, where shared deps/assets live, build/run/test, do/don'ts |
| 5 | **Git posture** | a remote + default branch exist, the repo **homepage** points at the deployed URL (`gh repo edit --homepage`), and ideally **PR CI** verifies PRs independently |
| 6 | **Report** | each item is PASS / FIXED / BOARDED, with the gate command — confirmed at a human gate |

## Procedure

1. **Scan (read-only).** With `read_file` / `list_dir` / `find_files`, detect:
   - the **stack + check command** — `package.json` scripts, `pyproject.toml`/`Makefile`/`justfile`, or a CI workflow (the most reliable source of "the real command");
   - whether `.beads/` exists; whether `.gitignore` ignores agent scratch + the build output dir;
   - a grounding doc — by convention `PROTO.md` (or its `CLAUDE.md` / `AGENTS.md` pointers, or a conventions section in the README);
   - the git remote, default branch, whether the repo **homepage** is set to the
     deployed-site URL (`gh repo view --json homepageUrl`), and any PR-triggered CI workflow.

2. **Auto-fix the safe, deterministic gaps** — one `delegate_to(coder, …)` with a precise brief to, only as needed:
   - `br init` (and commit) if there is no `.beads/` — **this is a bootstrap step, not a board feature** (the board can't hold a feature until beads exists);
   - add the coding agent's **per-session scratch** to `.gitignore` (commit) — for proto:
     `.proto/memory/`, `.proto/session-notes.md`, `.proto/repo-map-cache.json`. Do **not**
     blanket-ignore `.proto/`: its `evolve/` holds protoCLI-managed skills that should be
     versioned. (Don't add scratch dirs for tools this repo doesn't use.) Plus the build
     output dir.
   These are fast and judgment-free, so the coder does them directly rather than through a PR.

3. **Declare the gate.** Record the check command found in step 1. Ensure
   `project_board.local_gate_cmd` is set to it (e.g. `npm ci && npm run build`,
   `uv run pytest -q`). If you can't write the host config yourself, state the exact
   value for the operator to set — the gate is what makes the coder's PRs open
   already-green instead of bouncing through CI.

4. **Board the judgment gaps** — `board_create_feature` (+ `board_mark_ready`) for the
   work that needs real authoring + review, so it ships through the normal
   worktree→gate→PR loop:
   - a **grounding doc** — by convention **`PROTO.md`** (the canonical agent-instructions
     file; add thin `CLAUDE.md` + `AGENTS.md` pointers to it): conventions, the
     build/run/test commands, and — critically — **where shared dependencies/assets live
     and the rule to use the real source, never fabricate a lookalike.** This is the
     single highest-leverage item; it prevents the largest class of coder mistakes.
   - **PR CI** if missing, so PRs are verified independently, not only by the local gate.

5. **Human gate.** Summarize readiness (PASS / auto-FIXED / BOARDED feature ids) and
   call `request_user_input` to confirm before the team starts feature work.

6. **Report.** Output the checklist table with each item's status and the gate command.

## Rules

- **Never run the in-repo fixes yourself.** You have no shell; the coder carries file +
  shell access inside the repo/worktree. Delegate br init and gitignore edits to it.
- **Grounding beats gating.** A clear context doc prevents more failures than any gate —
  treat item 4 as required, not optional. The fabricated-asset / wrong-convention class
  of bug is a *grounding* gap, and `goal_verify` won't catch it if the acceptance
  criteria don't name the real source.
- **beads init is a bootstrap**, done via a direct coder delegate, before any feature —
  not a board feature (chicken-and-egg).
- **Idempotent.** Re-run whenever `loop-retro` surfaces a recurring failure a readiness
  item would have prevented — onboarding and retro are the two halves of the learning
  loop: retro finds the gap, onboarding encodes the fix so the next repo never hits it.
