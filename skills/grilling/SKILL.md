---
name: grilling
user_facing: true
slash: grill-me
description: >-
  Use when the operator runs /grill-me, asks you to grill them, or wants to stress-test a
  plan or design before anything is built. Interview them relentlessly, one question at a
  time, until you share an understanding of the plan. Nothing is created or dispatched
  until they confirm.
tools:
  - board_list             # is this plan already on the board?
  - board_get_feature      # an existing card's spec, when the plan touches one
  - github_read_file       # answer a question from the code instead of asking it
  - github_search_issues   # prior issues and decisions on the same subject
---

# Grilling

Interview the operator relentlessly about every part of the plan until you both understand
it the same way. Walk each branch of the design tree, and resolve dependencies between
decisions one by one.

- **One question at a time.** Wait for the answer before the next one. Several questions at
  once is bewildering.
- **Recommend an answer to every question.** Say what you'd choose and why, so the operator
  can answer "yes" or push back.
- **Don't ask what you can look up.** If the code, the board or an issue answers it, read it
  (`github_read_file`, `board_get_feature`, `github_search_issues`) and state what you
  found instead of asking.
- **Go where it's weakest.** Start with the decisions everything else depends on, then the
  ones most likely to be wrong: scope edges, failure modes, migration, what "done" means,
  who or what breaks if it ships.
- **Keep a running tally.** Every few questions, list what's settled and what's still open.

**Don't enact the plan** until the operator confirms you've reached a shared understanding.
That means no `board_create_feature`, no dispatch and no edits. Once they confirm, offer
the next step: usually a written summary, then cards via `board_create_feature` (or the
decompose-project skill for anything bigger than one card).
