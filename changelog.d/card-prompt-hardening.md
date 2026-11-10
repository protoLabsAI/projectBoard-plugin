- **Coders get fewer avoidable review rounds.** Measured on the live board (210 verified cards,
  2026-10-21..11-10): 36% bounced in review, and cross-file plus removed-behavior findings
  were 45% of all findings. Five changes target that:
  - **The coder prompt treats `files_to_modify` as a starting point, not a fence.** The
    heading is now "Files to start from (not a fence)". The coder may change the shared store,
    cache, hook or caller the fix belongs in, and names each unlisted file in its summary.
    "Done" now means the behavior is correct for every caller and shared state the change
    touches, not "the listed files exist".
  - **Every dispatch carries a pre-submit self-check.** Before it finishes, the coder traces the
    callers and shared state of what it changed, walks stale, failed-read and missing-record
    paths, and re-reads its deleted lines.
  - **A deliberate stop is handled as a claim, not a failed generation.** The prompt teaches the
    `BLOCKED-ON-SPEC:` line the loop already parses (#667). In best-of-k, a candidate that
    refuses passes its reasoning to its siblings ("check this claim or refute it") instead of a
    bare "no changes vs base". Only when every candidate refuses does the card block, under the
    `design` class, for the project manager (`SolveRefused`); no tier climb.
  - **The review gate gives the panel the card.** Its title, task, acceptance criteria and
    starting files go to the review workflow as a `card` input (an older recipe ignores it) and
    into the a2a reviewer prompt. A pre-existing issue outside the change, or behavior the card
    leaves out, is reported as a minor `out-of-scope` finding, never a blocker.
  - **Card-writing guidance.** `board-tool-reference` gains "What a coder can build from":
    acceptance criteria are observable behavior only, there are no stop clauses, shared state
    goes in `design`, and cards with 4+ authored files get split. `decompose-project` and
    `card-from-chat` point to it.
