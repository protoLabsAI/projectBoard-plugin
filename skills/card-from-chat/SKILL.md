---
name: card-from-chat
description: >-
  Use when the user asks in chat to add a feature, task, bug fix or idea to the board: "add a
  feature for …", "put … on the board", "make a card for …", "I want the agent to build …",
  "file this as a task". Call board_card_form right away: it shows a ready-made inline form
  the user fills in and confirms. Never write an HTML form, and don't ask for the fields in
  prose. When the form's message arrives ("Create board card · …"), create the card, mark it
  ready, open the Board on the main stage following it, and reply with one line.
tools:
  - board_card_form        # the ready-made inline form (a plugin frame component)
  - board_create_feature   # the card, from the values the form sent back
  - board_check_ready      # what still stops Ready, when board_create_feature's ready_check is not ok
  - board_update_feature   # fix those gaps in place
  - board_mark_ready       # backlog → on deck, so the loop claims it
  - board_watch            # open the Board view on the main stage, following the card
  - board_projects         # only if you need the project list outside the form
---

# A board card from chat

The user describes something they want built. You show them the board's **new-card form**
inline in the chat, they fill it in and press **Create card**, and the values come back to
you as their next chat message. Then you create the card and open the Board, following it
as the loop builds it.

The form is a fixed component the board plugin ships (`board-card-form`). You don't build
it, style it or write any HTML for it; you only call the tool. It is **not** the HITL / ask
/ approval tool, and not `show_artifact`. Don't call either here.

## 1. Show the form

Call `board_card_form` **right away**, as your first action:

- `title_hint`: a short, imperative title drawn from the request ("Add an undo command").
- `why_hint`: one line on what should change and why, in the user's words.
- `project`: only when the request names a project (or the conversation makes it obvious).
  Otherwise leave it empty; the form preselects the board's default.

The hints become the empty fields' placeholders. The user types the real values. The form
already lists the board's projects (and hides the picker when there's only one), so don't
call `board_projects` first.

Then write exactly one short line ("Fill this in and press Create card.") and nothing else:
no caveats, no notes on what might fail, no repeat of the fields in prose. Anything below
the form pushes it up the screen.

If the tool says the form can't render in this console, ask once, in one short message, for
the same fields it lists, then continue with step 2.

## 2. When the values arrive

The user's next message looks like this (the Project part is there when the board has one):

```
Create board card · <title>
Project: <name> · Priority: P<n>
Why: <what and why>
- <criterion>
- <criterion>
```

1. **Follow up only if you must.** Ask in plain chat **only** when the spec is genuinely
   ambiguous (two readings that would build different things) or a detail the build needs
   is missing. One short question, then wait. Otherwise don't ask anything: go straight on.
2. **Create.** Call `board_create_feature` with `project`, `title`, `priority` (the number
   from `P<n>`), `spec` (the `Why:` text) and `acceptance_criteria` (the `- ` lines, one per
   line). The form doesn't ask for paths, but the Ready gate needs `files_to_modify`. Work
   them out yourself with your read tools: read the project's repo and list the files the
   change touches, marking a file the card creates `(new)`. Add a `difficulty` if it's
   obvious.
3. **No unrequested dependencies.** Don't add dependency edges (`depends_on`, blockers) to
   other backlog cards unless the user asked for them. An edge to an unrelated, stale
   backlog chain leaves the new card waiting forever.
4. **Ready.** The build loop only claims cards that are **ready** (on deck), never backlog
   ones. If the create reply's `ready_check` is ok, call `board_mark_ready`. If not, fix
   each listed failure with `board_update_feature` (`board_check_ready` re-checks), then
   mark it ready. If it still refuses, leave the card in backlog and tell the user the one
   thing that blocks it.
5. **Watch.** Call `board_watch` with the new card id. It opens the Board view on the
   console's main stage and follows the card through on deck → building → in review → done.
6. **Reply with one line**, for example "Created bd-a1b2 and put it on deck; the Board is
   following it." Don't paste the card's fields back.

If the create is refused as a duplicate (an open card with the same title already exists),
don't force it. Call `board_watch` on the existing card and say so in one line.
