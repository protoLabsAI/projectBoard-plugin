---
name: showing-board-cards
description: >-
  Use for any status check-in on board work: "what's blocked?", "how's the epic?", "how's
  epic X going?", "status?", "status update", "check in", "where are we?", "what's in
  review?", "what's left?", "progress on bd-…?". Answer with the live cards themselves:
  call board_show_cards scoped to the epic or the cards in question, then write AT MOST
  TWO LINES of prose — never restate the cards. Also use when a message arrives from a card button, like "Unblock
  board card bd-…", "Why is board card bd-… blocked?", "Ask Vera to re-review board card
  bd-…", "Merge board card bd-…", "Show the live coder run for board card bd-…" or
  "Dispatch board card bd-…". Each one maps to a board_* tool below. Not for your own
  reasoning about the board; read with board_list / board_get_feature for that.
tools:
  - board_show_cards       # render the live cards inline (+ a text summary you read)
  - board_list             # find a check-in's cards; with_ci=true before a merge
  - board_get_feature      # one card's full detail: blocked_reason, next_action, pr_url, …
  - board_comments         # a card's history: why it blocked, what the coder did
  - board_unblock_feature  # "Unblock board card …"
  - board_check_ready      # "Dispatch board card …" on a backlog card: what stops Ready
  - board_mark_ready       # …and promote it when nothing does
  - board_dispatch         # "Dispatch board card …": ask the loop to claim now
---

# Showing board cards in chat

A status check-in is answered with **the cards**, not paragraphs about them.
`board_show_cards` puts live cards in the transcript. Each one shows its state,
`next_action`, block reason and PR link, plus buttons that act on it. The cards re-read
themselves every ~15 s, so an answer stays true after you've finished writing it.

## Answering a check-in

1. **Scope it.** Show the cards the question is about.
   - "How's the board?", "status?", "check in", "where are we?" → `board_show_cards()` with
     **no arguments**: all open work (every state but done), most urgent first. Don't split
     it into one call per state — that drops the blocked and queued cards.
   - "What's blocked?" → `board_show_cards(state="blocked")`.
   - "What's in review on protoAgent?" → `board_show_cards(state="in_review", project="protoAgent")`.
   - "How's epic #4076?" → find the epic's cards with `board_list` (match the epic's
     children / title prefix), then `board_show_cards(ids=[…], title="Epic #4076")`.
     Pass at most 20 ids. For a larger epic, show the open ones.
   - "Where's bd-x?" → `board_show_cards(ids=["bd-x"])`.
2. **Then at most two lines of prose.** Say what the cards can't: the one thing that needs
   the operator, or the call you'd make. Don't restate rows the cards already show.
   - Good: "Two are waiting on you: bd-a1 needs a merge (auto_merge is off), and bd-c3
     is blocked on a missing secret."
   - Bad: a bullet per card repeating its state.
3. The tool's text reply (a header and one line per card) is for **you**, and for clients
   that can't render cards. Use it to write your two lines; don't paste it back.

## When NOT to use it

- **Your own reasoning.** Picking the next card, checking dependencies, planning a
  decomposition: read with `board_list` / `board_get_feature`. Cards in chat are for the
  operator. Calling this for yourself spams the transcript.
- **Every turn.** One set of cards per check-in. Don't re-show the same cards after an
  action unless the operator asks; the cards already on screen update themselves.
- **Detail questions.** "What does bd-x's spec say?" or "what were the review findings?"
  need text from `board_get_feature` / `board_comments`, not a card.
- **Unattended runs.** Scheduled or A2A-only runs have no operator looking. Answer in
  text.

## Acting on a card button

Every button sends an exact sentence as a normal user message, after the operator
confirms it in the chip. Treat it as the operator's instruction, and act with the
matching tool. Always re-read the card first, because the button may be older than the
card's current state. If the card has moved on, say so in one line instead of acting.

| Message | Do this |
|---|---|
| `Unblock board card bd-…` | `board_get_feature` → read `blocked_reason` / `blocked_class`. Then `board_unblock_feature(bd-…)`. A card blocked out of review re-arms its review gate by default. Pass `override_review=true` only if the operator explicitly accepts the findings. A `preflight-hold` is the project's gate failing on base, not the card. Say so; unblocking won't help. |
| `Why is board card bd-… blocked?` | `board_get_feature` (`blocked_reason`, `blocked_class`, `preflight_failure`) plus `board_comments` for the history. Answer in two or three lines with the cause and the fix. **Don't change anything.** |
| `Ask Vera to re-review board card bd-…` | `board_get_feature` → `pr_url`. Ask the fleet reviewer for a fresh pass on that PR, the way this agent normally summons review (the `@vera review` PR comment, or `delegate_to` the reviewer with the PR URL). **Don't** `board_requeue_feature`; that sends the card back to a coder. |
| `Merge board card bd-…` | Re-check the gates: `board_get_feature` must show `in_review` and `awaiting_merge: true`, and `board_list(state="in_review", with_ci=true)` must show its CI passing. Then merge its PR with this agent's GitHub tooling. If you have none, tell the operator to merge #N, or to turn `auto_merge` on. The merge moves the card to done by itself; **never** `board_mark_done` a merged-PR card. |
| `Show the live coder run for board card bd-…` | `board_get_feature` (state, attempt) and the latest `board_comments` (the coder-monitor notes). Summarize where the run is in two lines. The Board view's monitor drawer has the live tool feed; point there for more. |
| `Dispatch board card bd-…` | A `backlog` card: `board_check_ready`, fix what it lists or report it, then `board_mark_ready`. Then `board_dispatch`. It claims by priority and capacity, so report its `outcome` honestly. If it claimed a different card, or was `at-capacity` / `held`, say so. |

**After acting, always confirm in ONE line** — never end the turn on tool calls alone. Say
what happened and the card's new state, e.g. "Unblocked bd-6k3; it's back in backlog." or
"Didn't unblock bd-6k3: its project's gate is failing on base (ruff F841)." The card on
screen shows the new state on its next refresh.
