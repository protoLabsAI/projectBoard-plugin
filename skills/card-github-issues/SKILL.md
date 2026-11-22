---
name: card-github-issues
description: >-
  Use when asked to put GitHub issues on the board: "card these issues", "card sprint 2",
  "get #12, #15 and #19 onto the board", "start the sprint", or any plan that lists issues to
  build. Each issue goes to a `board_card_writer` helper running in the background, so your
  chat stays free and your context stays small. You do not read the issues or the code
  yourself. For one card the user describes in chat, use card-from-chat instead.
tools:
  - task_batch             # one background helper per issue, in a single call
  - task                   # a single issue, also in the background
  - board_list             # duplicates before you start, and the result when helpers finish
  - board_get_feature      # one card's detail when a helper reports a problem
  - board_update_feature   # add the dependency edges between cards from different issues
  - board_check_ready      # after adding an edge
  - board_mark_ready       # release cards to the loop
  - request_decision       # a question only the operator can answer
---

# Card GitHub issues without holding the chat

Carding an issue means reading it, reading the code it names, writing a spec and repairing
what the Ready gate reports. That is a lot of reading. Done in your own thread it fills your
context and holds your chat for the whole time: messages sent to you queue behind it, and
after a few issues you lose the thread, stop early, or create the same card twice.

So you do not do that reading. A `board_card_writer` helper does it, one helper per issue,
in the background. You keep the three things that need the whole picture.

## 1. Start the helpers, then answer

- Call `board_list` once for each project involved and note which issues already have an
  open card. Leave those out.
- Send every remaining issue in **one** `task_batch` with `run_in_background=true`: one
  `board_card_writer` spec per issue. A single issue is one `task` with
  `run_in_background=true`.
- Each helper's prompt is the issue (`owner/repo#N`), the board project it builds in, and any
  instruction the user gave about that issue, in their words. Nothing else: do not paste the
  issue body, and do not summarise the code for it.
- **Reply straight away** with what you started: the issues, and that the cards will follow.
  Then end the turn. Do not wait for the helpers, and do not read the issues "to get ahead".

More than eight issues: send them in batches of eight, the next batch when one reports back.

If every helper fails at startup with the same error (a model the host cannot run, a tool
that is not bound), do not card the issues in your own thread instead. Say what failed, once,
and ask the operator with `request_decision`.

## 2. When the helpers report back

Each returns a few lines: `CARD <id> | ready_check: … | <title>`, a `FILES <id>: …` line per
card, or `EXISTING`, `NO CARD` or `NEEDS A DECISION`. Work only from those lines.

- **Duplicates.** If two helpers produced cards for the same change, keep one and cancel the
  other with a reason.
- **Order.** Two cards from different issues that list the same file must not build at the
  same time. Add a `depends_on` edge from the later one to the earlier one, and say on the
  card which file they share. An edge needs that reason; "it feels related" is not one.
- **Not ok.** A card whose `ready_check` is not ok goes back to a helper with what blocks it
  (`task`, in the background). Do not repair it in your own thread.
- **A decision.** Ask the operator with `request_decision`: one plain question with distinct
  answers. The other cards do not wait for it.

## 3. Release, and say what happened

- `board_mark_ready` every card that is ok and has no open blocker, unless the user asked for
  backlog. A slice of a multi-card issue is released when the card before it is.
- Reply with the list: issue → card id(s) → ready or why not. Name every issue you were
  asked for, including the ones with no card.

## What you never do here

- Read an issue body, its comments or source files to write a card yourself.
- Create a card with `board_create_feature` while helpers are running for that issue.
- Wait in the turn for a background helper.
- Close an issue. A merged PR closes it; an issue with several cards closes on its last one.
