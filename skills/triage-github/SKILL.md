---
name: triage-github
user_facing: true
slash: triage-github
description: >-
  Use when the operator runs /triage-github or wants a GitHub issue queue triaged,
  prioritized or organized, or asks "what should I work on next?". Score each open issue on
  Value, Level of Effort and Risk, rank by value per effort, and bucket the queue into quick
  wins, big bets, fill-ins and reconsider. Read-only unless the operator asks for writes.
tools:
  - board_projects         # which repos the board manages (the default scope)
  - board_list             # issues that already have a card are not re-triaged
  - github_list_issues     # the open queue (limit up to 50 per call)
  - github_search_issues   # scope or page the queue: label:, milestone:, sort:created
  - github_get_issue       # one issue's full body, when the listing is too thin to score
  - github_issue_comments  # demand and context on an issue
  - github_set_labels      # write-back, only when asked
  - github_comment         # write-back, only when asked
  - board_create_feature   # turn chosen quick wins into cards, only when asked
---

# Triage the issue queue by Value × LOE × Risk

Turn an unordered open-issue queue into a ranked, bucketed worklist. Every open issue gets
three scores, **Value**, **LOE** (level of effort) and **Risk**, and is ranked by value per
effort. Risk is a flag, so the output says both what to do next and how carefully.

## Process

### 1. Pull the queue

- **Scope.** Use the repo the operator named. Otherwise use the board's projects
  (`board_projects`) and ask which repo if there's more than one.
- **Fetch.** `github_list_issues(state="open", limit=50)`. If the operator passed filters
  (a label, a milestone, an assignee), use `github_search_issues` with that query instead.
  A full page of 50 means there are more. Page with `github_search_issues` (for example
  `is:open sort:created-asc`) until you have them all, or offer to scope by label or
  milestone first. A triage of more than ~40 issues that nobody reads does nothing.
- **Skip what's already on the board.** Cross-check `board_list()`. An issue a card already
  carries (its `source_issue`, or the issue linked in its spec) is in flight. List it at
  the end under "Already on the board", without scoring it.

### 2. Read each issue well enough to score it

Score from the title, body, labels, age, comment count and reactions. Open an issue
(`github_get_issue`, `github_issue_comments`) only when its body is truncated or too vague
to score. Don't guess: an issue you can't score is `needs-info`, not a low score.

### 3. Score Value, LOE and Risk with the rubric below

For each score, keep one **signal phrase**, the evidence behind it (a label, a scope word,
an area of code), so the ranking can be checked.

### 4. Rank and bucket

- **Priority = Value ÷ LOE** (both 1–5). Higher ranks sooner.
- **Risk never demotes** a high-value fix. It says how to land it (careful review, a draft
  PR, live verification). Anything with High risk gets the ⚠️ flag.
- Put each issue in exactly one bucket.

### 5. Present, then offer next steps

Show the output below. Only if the operator asks, offer write-back: `github_set_labels`
(`priority:*` or `triage`), `github_comment` with the ranking, or `board_create_feature`
cards for the chosen quick wins, each with a spec built from the issue. Confirm the exact
writes before making any.

**Done when** every pulled issue appears exactly once, with all three scores and a bucket,
or under "Already on the board". Nothing is silently dropped.

## The rubric

**Value (1–5).** How much good does resolving it do?

| | |
|---|---|
| 5 | Launch-blocking, security or data loss, or unblocks several other issues |
| 4 | A clear user-facing bug or a feature in high demand |
| 3 | A meaningful improvement with moderate demand |
| 2 | Nice to have, narrow audience |
| 1 | Cosmetic or speculative |

Signals: `bug`, `P0`, `P1`, `critical` or `priority:high` labels; comments and reactions
(demand); "blocks" or "unblocks"; security or data words.

**LOE (1–5).** Effort to resolve, from 1 (trivial) to 5 (large).

| | |
|---|---|
| 1 | One line, config or docs |
| 2 | Localized: a few files, a clear path |
| 3 | Several files, some design and tests |
| 4 | Cross-cutting: a migration or several subsystems |
| 5 | Unclear scope, needs design, or spans repos |

Signals: the scope in the body; "migration", "ADR", "design" or "rework"; how many
subsystems it names; open questions. Unscoped (`needs-info`) means high LOE until scoped.

**Risk (Low / Med / High).** The chance the fix regresses something, times the damage.

| | |
|---|---|
| High | Core hot paths, shared invariants, release or security, or hard to verify |
| Med | A subsystem others depend on; broad but verifiable |
| Low | Isolated, well tested, easy to revert |

Prefer the repo's own risk labels. In **protoAgent**: `graph/`, `server/`, the
import-layering contract, golden or config-roundtrip tests, the release pipeline and auth
are **High**; UI-only is **Med** (CI can't judge UX, so land it as a draft PR); docs and
config are **Low**.

## Buckets

| Bucket | Rule | Action |
|---|---|---|
| 🟢 Quick win | high Value ÷ LOE, LOE ≤ 2 | do now |
| 🔵 Big bet | Value ≥ 4, LOE ≥ 4 | plan first |
| 🟡 Fill-in | Value ≤ 2, LOE ≤ 2 | batch when nearby |
| 🔴 Reconsider | Value ≤ 2, LOE ≥ 4 | close, defer or `needs-info` |
| ⚠️ Risk-gated | Risk = High (any bucket) | careful review, draft PR |

## Output

Skimmable lists, not a table. Group by bucket in this order, one line per issue:

```
**#n** short title — V?/LOE? · Risk · one-clause why
```

- **🟢 Do next**: quick wins, plus high-value LOE-3 work
- **⚠️ High value, handle with care**: high value with High risk
- **🔵 Big bets: plan first**
- **🟡 Fill-in**
- **🔴 Reconsider or close**
- **Already on the board**: `#n → bd-…`, no scores

Keep each line to one terminal row. The Value/LOE/Risk triple and the reason carry the
reasoning, so don't add per-axis columns.
