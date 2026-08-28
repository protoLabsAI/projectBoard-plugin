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
  - board_register_project   # register the repo as a board project — ONLY offered when onboarding.enabled
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
| 3 | **Gate** | the repo **declares** its gate target (a `gate`/`ci`/`check`/`verify` script or Makefile/justfile target) so the board's `"auto"` discovery resolves it |
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

3. **Declare the gate.** Record the check command found in step 1 for the report —
   you never pass a gate command anywhere; no tool you hold takes one. The board
   discovers the gate from the repo's **own declared target**: registration (step 5)
   passes the literal `gate="auto"`, and the loop resolves it from a
   `gate`/`ci`/`check`/`verify` script (package.json) or Makefile/justfile target.
   If the repo declares none, board a feature to add one (e.g. a `gate` target
   running the step-1 command), and name the command in the report so the operator
   can alternatively set `project_board.local_gate_cmd` themselves (Settings ▸
   Projects / YAML — operator config, not yours to write). The gate is what makes
   the coder's PRs open already-green instead of bouncing through CI.

4. **Board the judgment gaps** — `board_create_feature` (+ `board_mark_ready`) for the
   work that needs real authoring + review, so it ships through the normal
   worktree→gate→PR loop:
   - a **grounding doc** — by convention **`PROTO.md`** (the canonical agent-instructions
     file; add thin `CLAUDE.md` + `AGENTS.md` pointers to it): conventions, the
     build/run/test commands, and — critically — **where shared dependencies/assets live
     and the rule to use the real source, never fabricate a lookalike.** This is the
     single highest-leverage item; it prevents the largest class of coder mistakes.
   - **PR CI** if missing, so PRs are verified independently, not only by the local gate.

5. **Human gate — the readiness/registration form.** Summarize readiness
   (PASS / auto-FIXED / BOARDED feature ids) and call `request_user_input` to confirm
   before the team starts feature work. Whether the form offers **registration** is
   gated on the HOST's `onboarding.enabled` setting — check it BEFORE building the
   form (it is host config, not repo state: Settings ▸ Project onboarding; if you
   cannot confirm it is true, treat it as off — `board_register_project` fails closed
   the same way):
   - **onboarding enabled** → include a `register_project: true/false` field asking
     whether to register this repo as a board project (`board_register_project`) so
     features can be dispatched here.
   - **onboarding off** → **omit the `register_project` field entirely** — never offer
     a choice the host will refuse — and add this note to the form description:
     *"Project registration unavailable — enable Settings ▸ Project onboarding to
     register repos for filesystem access."*

   If the operator answered `register_project: true`, call
   `board_register_project(name, repo, base_branch, gate="auto",
   repo_conventions=…)` before reporting — the literal `"auto"` (repo-declared
   target discovery) is the only gate value the tool accepts; it takes no
   command text.

6. **Report.** Output the checklist table with each item's status and the gate command.
   If registration was attempted and the tool **refused** (an `Error:` return — e.g. a
   `true` answer from a form built before this gate existed, on a host with onboarding
   off), the refusal is the **first line** of the completion summary —
   `⚠ REGISTRATION REFUSED: <the tool's error, verbatim>` — never buried mid-report:
   the operator must see that their answer did not take effect and what to enable.

## Rules

- **Never run the in-repo fixes yourself.** You have no shell; the coder carries file +
  shell access inside the repo/worktree. Delegate br init and gitignore edits to it.
- **Grounding beats gating.** A clear context doc prevents more failures than any gate —
  treat item 4 as required, not optional. The fabricated-asset / wrong-convention class
  of bug is a *grounding* gap, and `goal_verify` won't catch it if the acceptance
  criteria don't name the real source.
- **beads init is a bootstrap**, done via a direct coder delegate, before any feature —
  not a board feature (chicken-and-egg).
- **Never offer what the host will refuse.** The `register_project` field appears in
  the human-gate form only when `onboarding.enabled` is true; when it's off the form
  carries the unavailable-note instead. If a `true` answer arrives anyway (an older
  form), the refusal leads the completion summary — an operator's answer must never
  silently evaporate.
- **Idempotent.** Re-run whenever `loop-retro` surfaces a recurring failure a readiness
  item would have prevented — onboarding and retro are the two halves of the learning
  loop: retro finds the gap, onboarding encodes the fix so the next repo never hits it.
