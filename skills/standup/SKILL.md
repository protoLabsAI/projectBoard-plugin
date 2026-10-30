---
name: standup
user_facing: true
slash: standup
description: >-
  Use when the operator runs /standup or asks for a "standup", a progress report or a
  written status report on the work you've been owning: epics and their phases, open PRs,
  the critical path, what's gated, and what needs the operator's call. A structured,
  written report. For a quick "status?" or "check in", use showing-board-cards instead:
  the live cards answer it.
tools:
  - board_list             # every card, priority order; filter by state / project
  - board_get_feature      # one card: blocked_reason, next_action, pr_url, depends_on
  - board_comments         # why a card blocked, what the last round did
  - board_projects         # the registered projects to cover
  - github_list_prs        # open PRs with review decision + merge state
  - github_get_pr          # one PR's checks and merge-readiness
  - github_issue_comments  # the review verdict on a PR
---

# Standup

A written status report on the work you own, built for a fast scan: done, in flight,
blocked, and what needs the operator. The board is your plan of record. Its epics are
the phases, its cards are the items, and its states are the status.

## 1. Gather (read everything before writing)

- **The plan.** `board_list()` gives every card. Group cards under their epic (parent);
  each epic is a phase. Cards with no epic go under "Unplanned". If the operator named an
  epic or a project, scope to it.
- **What moved.** Note cards done since the last standup or the last ~24h (their
  `updated_at`), and what's `in_progress`, `in_review` and `blocked`.
- **Why things are stuck.** For each blocked card and each card whose `next_action` names
  the operator, read `board_get_feature` (and `board_comments` when the reason is unclear).
  Don't guess a cause.
- **Open PRs.** `github_list_prs(state="open")` on each project's repo. Mark each PR as
  the board's (it matches a card's `pr_url`), the operator's, or someone else's. For a
  board PR that isn't merging, `github_get_pr` shows its checks and merge state.

## 2. Write the report

Start with a phase table: one row per epic, a 10-block bar, and a one-line note.

```
Phase                                   Status        Notes
──────────────────────────────────────────────────────────────────────
ADR 0125 memory split (bd-ab12)         ▓▓▓▓▓▓▓░░░    Phase 1 merged; 2 cards ready
Eval flywheel (bd-1cky)                 ▓▓▓▓▓▓▓▓▓▓    complete
```

Bars: `░░░░░░░░░░` not started · `▓░░░░░░░░░` specced · `▓▓▓▓▓░░░░░` half the cards
done · `▓▓▓▓▓▓▓▓▓░` tail only · `▓▓▓▓▓▓▓▓▓▓` complete. Weigh by card size, not just
count. A card that merged but isn't released yet is not 100% for the operator. Say so.

Then, for each phase with activity, list its cards:

- ✅ done (merged / verified), with the PR link
- 🟡 in flight: the state and what it's waiting on ("in review, QA panel running")
- ⬜ not started (backlog / ready)
- ⬛ cancelled or deferred, with the reason
- 🚨 blocked and needs unblocking now. Use it sparingly.

Close with these sections, always in this order. Write "none" when one is empty, so the
operator knows nothing is hiding:

- **Open PRs**: `#N title (board | operator's | other) — status`.
- **Critical path**: what stands between now and the next milestone, by card and PR ID.
- **What I can do meanwhile**: unblocked work you can start without the operator.
- **Gated on the operator or something external**: each item and exactly what's needed.
- **Needs your call**: each pending decision, with your recommendation.

## Rules

- Name everything by ID (`bd-…`, `#N`). "Blocked on #4629" beats "blocked on the env fix".
- Don't oversell. A merged seam with no consumer isn't a shipped feature. Show the gap.
- Report what the tools returned. If a read failed, say which one and treat that part as
  unknown.
- The report goes in chat. Don't write it to a file or a card unless asked.
