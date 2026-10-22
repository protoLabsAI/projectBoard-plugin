---
name: card-from-chat
description: >-
  Use when the user asks in chat to add a feature, task, bug fix or idea to the board: "add a
  feature for …", "put … on the board", "make a card for …", "I want the agent to build …",
  "file this as a task". Answer with a small inline FORM (an interactive answer the user fills
  in and sends back), not with questions in prose and not with the ask/approval tool. When the
  form's message arrives ("Create board card: …"), create the card, mark it ready, open the
  Board on the main stage following it, and reply with one line.
tools:
  - board_projects         # the registered project names for the form's picker
  - show_artifact          # the inline form (artifact plugin, ADR 0118 interactive answer)
  - board_create_feature   # the card, from the values the form sent back
  - board_check_ready      # what still stops Ready, when board_create_feature's ready_check is not ok
  - board_update_feature   # fix those gaps in place
  - board_mark_ready       # backlog → on deck, so the loop claims it
  - board_watch            # open the Board view on the main stage, following the card
---

# A board card from chat

The user describes something they want built. You hand them a small form inline in the
chat, they fill it in and press **Create card**, and the filled values come back to you as
their next chat message. Then you create the card and open the Board, following it as the
loop builds it.

This is an **interactive answer** (protoAgent ADR 0118): an inline `html` artifact whose
button calls `window.protoArtifact.send(text)`. The console shows the user a "Send to chat"
confirm chip, and on **Send** the text lands as an ordinary user turn. It is **not** the
HITL / ask / approval tool. Don't call that tool here, and don't ask for the fields one at a
time in prose.

## 1. Build the form

1. Call `board_projects` for the project names and the default.
2. Call `show_artifact(kind="html", title="New board card", code=…)`. It renders inline in
   the chat by default (don't pass `placement="panel"`).
3. Leave every text field **empty**: the user fills in the spec, so don't type a draft into
   the values. Give each field a short `placeholder` instead, an example drawn from what
   they said. The selects keep their defaults (the default project, P2).

The form has these fields, in this order:

| Field | Control | Notes |
|---|---|---|
| Project | `<select>` | one `<option>` per `board_projects` name, with the default `selected`; omit the field when there is only one project |
| Title | `<input>` | required; short, imperative ("Add an undo command") |
| What and why | `<textarea rows="3">` | required; the problem and the reason, in the user's words |
| Acceptance criteria | `<textarea rows="3">` | required; one testable criterion per line |
| Priority | `<select>` | optional; P0 to P4, default P2 |

Keep it small and tidy, because it may be on camera. Use one `pl-card`, a single column that
fills the card's width (each label stacked over a full-width field), compact spacing, no heading beyond a one-line `pl-eyebrow`, and one primary
button. Colours come only from `--pl-*` tokens, and the kit classes (`pl-field`, `pl-input`,
`pl-select`, `pl-textarea`, `pl-btn pl-btn--primary`) theme everything. Author it `<style>`
first, then markup that reads as finished before any script runs, then `<script>` last.

The button:

- **Feature-detects** `window.protoArtifact?.send`. Without it, it shows the message text so
  the user can copy it.
- **Validates** first. The title, the "what and why" and at least one criterion must be
  non-empty. Otherwise it marks the empty field and sends nothing.
- **Sends every value in the text**, because you see only the text, never the form's state.
  Use exactly this shape, so step 2 can read it back:

  ```
  Create board card:
  Project: <name>
  Title: <title>
  Priority: P<n>
  What and why:
  <text>
  Acceptance criteria:
  - <criterion>
  - <criterion>
  ```

- **Handles the Promise.** It says "Sent" only once `send` resolves. A dismissal, "the agent is
  busy" or a rate limit is a quiet "Not sent: …" line, never an error. It disables itself
  while a send is in flight.

A skeleton to adapt (swap in the real project options and placeholders drawn from the
user's request; keep the values empty):

```html
<style>
  .cf { display: grid; gap: var(--pl-space-3); width: 100%; box-sizing: border-box; }
  .cf .row { display: grid; grid-template-columns: 1fr 120px; gap: var(--pl-space-3); align-items: end; }
  .cf .pl-field { display: grid; gap: var(--pl-space-1); min-width: 0; }
  .cf .pl-input, .cf .pl-select, .cf .pl-textarea { width: 100%; box-sizing: border-box; }
  .cf textarea { resize: vertical; }
  .cf .foot { display: flex; align-items: center; gap: var(--pl-space-3); }
  .cf .note { color: var(--pl-color-fg-muted); font-size: 12px; }
  .cf [aria-invalid="true"] { border-color: var(--pl-color-status-error); }
</style>
<form class="cf pl-card" id="cf" novalidate>
  <div class="pl-eyebrow">New board card</div>
  <div class="row">
    <div class="pl-field"><label for="project">Project</label>
      <select id="project" class="pl-select"><option selected>protoagent</option></select></div>
    <div class="pl-field"><label for="pri">Priority</label>
      <select id="pri" class="pl-select"><option>P0</option><option>P1</option>
        <option selected>P2</option><option>P3</option><option>P4</option></select></div>
  </div>
  <div class="pl-field"><label for="title">Title</label>
    <input id="title" class="pl-input" required placeholder="e.g. Add an undo command"></div>
  <div class="pl-field"><label for="why">What and why</label>
    <textarea id="why" class="pl-textarea" rows="3" required placeholder="What should change, and why it matters"></textarea></div>
  <div class="pl-field"><label for="ac">Acceptance criteria <span class="note">(one per line)</span></label>
    <textarea id="ac" class="pl-textarea" rows="3" required placeholder="One testable result per line"></textarea></div>
  <div class="foot">
    <button class="pl-btn pl-btn--primary" id="go" type="submit">Create card</button>
    <span class="note" id="status" role="status"></span>
  </div>
</form>
<script>
  const $ = (id) => document.getElementById(id);
  $("cf").addEventListener("submit", async (e) => {
    e.preventDefault();
    const v = { project: $("project").value, pri: $("pri").value, title: $("title").value.trim(),
      why: $("why").value.trim(),
      ac: $("ac").value.split("\n").map((l) => l.replace(/^[\s*-]+/, "").trim()).filter(Boolean) };
    let bad = false;
    for (const [id, ok] of [["title", v.title], ["why", v.why], ["ac", v.ac.length]]) {
      $(id).setAttribute("aria-invalid", ok ? "false" : "true"); bad = bad || !ok;
    }
    if (bad) { $("status").textContent = "Fill in the marked fields."; return; }
    const msg = ["Create board card:", "Project: " + v.project, "Title: " + v.title,
      "Priority: " + v.pri, "What and why:", v.why, "Acceptance criteria:",
      ...v.ac.map((c) => "- " + c)].join("\n");
    if (typeof window.protoArtifact?.send !== "function") { $("status").textContent = msg; return; }
    $("go").disabled = true; $("status").textContent = "Waiting for you to confirm…";
    try { await window.protoArtifact.send(msg); $("status").textContent = "Sent"; }
    catch (err) { $("status").textContent = "Not sent: " + (err && err.message || err); $("go").disabled = false; }
  });
</script>
```

After `show_artifact`, write exactly one short line ("Fill this in and press Create card.") and
nothing else: no caveats about projects or tools, no notes on what might fail, and no repeat of
the fields in prose. Anything below the form pushes it up the screen. If something does go
wrong, say so after the values come back.

## 2. When the values arrive

The user's next message starts with `Create board card:`. Then:

1. **Create.** Call `board_create_feature` with `project`, `title`, `priority` (the number
   from `P<n>`), `spec` (the "What and why" text) and `acceptance_criteria` (the criteria,
   one per line). The form doesn't ask for paths, but the Ready gate needs
   `files_to_modify`. Work them out yourself: read the project's repo with your file tools
   and list the files the change touches, marking a file the card creates `(new)`. Add a
   `difficulty` if it's obvious.
2. **Ready.** The build loop only claims cards that are **ready** (on deck), never backlog
   ones. If the create reply's `ready_check` is ok, call `board_mark_ready`. If not, fix
   each listed failure with `board_update_feature` and then mark it ready. If it still
   refuses, leave the card in backlog and tell the user the one thing that blocks it.
3. **Watch.** Call `board_watch` with the new card id. It opens the Board view on the
   console's main stage and follows the card through on deck → building → in review → done.
4. **Reply with one line**, for example "Created bd-a1b2 and put it on deck; the Board is
   following it." Don't paste the card's fields back.

If the create is refused as a duplicate (an open card with the same title already exists),
don't force it. Call `board_watch` on the existing card and say so in one line.
