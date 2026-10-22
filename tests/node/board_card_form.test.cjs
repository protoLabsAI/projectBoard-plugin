// Page logic for the new-card form in chat (board-card-form), under node — no DOM, no host.
// Run by tests/test_board_card_form.py (`node --test`), or directly:
//   node --test tests/node/board_card_form.test.cjs
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const BF = require(path.join(__dirname, "..", "..", "component", "board-card-form.js"));

const NONCE = "0123456789abcdef0123456789abcdef";

function harness(raw) {
  const posts = [];
  const renders = [];
  const statuses = [];
  const marks = [];
  const busy = [];
  let locked = false;
  const values = Object.assign({ project: "", title: "", why: "", criteria: "", priority: "P2" }, raw || {});
  const ctl = BF.createController({
    post: (m) => posts.push(m),
    render: (h) => renders.push(h),
    applyTheme: () => {},
    read: () => values,
    markInvalid: (b) => marks.push(b),
    setStatus: (t) => statuses.push(t),
    setBusy: (b) => busy.push(b),
    lock: () => { locked = true; },
    setTimeout: () => 0,
  });
  return { ctl, posts, renders, statuses, marks, busy, values, isLocked: () => locked };
}

function init(h, props) {
  h.ctl.onMessage({ type: "protoComponent:init", props: props || {}, theme: {}, nonce: NONCE });
}

// ── rendering ───────────────────────────────────────────────────────────────────────

test("several projects: a picker with the selected one; one project: no picker, named in the eyebrow", () => {
  const many = BF.renderHTML({ projects: ["web", "api"], project: "api" });
  assert.match(many, /<select id="cf-project"/);
  assert.match(many, /<option value="api" selected>api<\/option>/);
  assert.match(many, /New board card</);

  const one = BF.renderHTML({ projects: ["demo"], project: "demo" });
  assert.doesNotMatch(one, /cf-project/);
  assert.match(one, /New board card · demo/);

  const none = BF.renderHTML({});
  assert.doesNotMatch(none, /cf-project/);
});

test("every field is a stacked label over a full-width control, empty, with placeholders", () => {
  const html = BF.renderHTML({ title_hint: "Add CSV export", why_hint: "Ops want the runs as a file" });
  for (const id of ["title", "why", "criteria", "priority"]) {
    assert.match(html, new RegExp('<label class="pl-field__label" for="cf-' + id + '"'));
  }
  assert.match(html, /placeholder="e\.g\. Add CSV export"/);
  assert.match(html, /placeholder="Ops want the runs as a file"/);
  assert.doesNotMatch(html, /<input[^>]*value=/, "the title starts empty");
  assert.match(html, /<textarea id="cf-why"[^>]*><\/textarea>/, "the textareas start empty");
  assert.match(html, /<option value="P2" selected>/);
  assert.doesNotMatch(html, /<form/);
  assert.match(html, /<button type="button" class="pl-btn pl-btn--primary" id="cf-create">Create card<\/button>/);
});

test("props are escaped and untrusted names dropped", () => {
  const html = BF.renderHTML({ projects: ["ok", "<b>x</b>"], title_hint: '"><script>alert(1)</script>' });
  assert.doesNotMatch(html, /<script>/);
  assert.doesNotMatch(html, /<b>x/);
  assert.match(html, /&quot;&gt;&lt;script&gt;/);
});

// ── values → the proposal ─────────────────────────────────────────────────────────────

test("criteria parse one per line with list markers stripped", () => {
  assert.deepEqual(BF.parseCriteria("- one\n\n* two\n3. three\n  • four  \n[ ] five\n"), ["one", "two", "three", "four", "five"]);
});

test("the message is compact and machine-readable", () => {
  const v = BF.collect(
    { project: "web", title: "  Add CSV export ", why: "Ops need the runs\nas a file.", criteria: "- Button downloads a CSV\n- Columns match the table", priority: "P1" },
    { projects: ["web", "api"], project: "web" },
  );
  const msg = BF.buildMessage(v);
  assert.equal(
    msg,
    [
      "Create board card · Add CSV export",
      "Project: web · Priority: P1",
      "Why: Ops need the runs as a file.",
      "- Button downloads a CSV",
      "- Columns match the table",
    ].join("\n"),
  );
  assert.ok(msg.split("\n").length <= 8, "fits the confirm chip");
});

test("a tampered project or priority falls back to the props' choice and P2", () => {
  const v = BF.collect({ project: "evil", title: "t", why: "w", criteria: "c", priority: "P9" }, { projects: ["web", "api"], project: "api" });
  assert.equal(v.project, "api");
  assert.equal(v.priority, "P2");
});

test("validation names each missing field", () => {
  assert.deepEqual(Object.keys(BF.validate(BF.collect({ title: "", why: " ", criteria: "\n- \n" }, {}))).sort(), ["criteria", "title", "why"]);
  assert.deepEqual(BF.validate(BF.collect({ title: "t", why: "w", criteria: "c" }, {})), {});
  const many = Array.from({ length: BF.CRITERIA_MAX + 1 }, (_, i) => "c" + i).join("\n");
  assert.ok(BF.validate(BF.collect({ title: "t", why: "w", criteria: many }, {})).criteria);
});

// ── the controller: click → validate → propose → verdict ───────────────────────────────

test("an empty click marks the fields and sends nothing", async () => {
  const h = harness();
  init(h, { projects: ["demo"], project: "demo" });
  assert.equal(h.renders.length, 1);
  await h.ctl.create();
  assert.equal(h.posts.length, 0);
  assert.deepEqual(Object.keys(h.marks[0]).sort(), ["criteria", "title", "why"]);
  assert.equal(h.statuses.at(-1), "Fill in the marked fields.");
});

test("Create card proposes ONE protoComponent:send with the nonce, then reports Sent and locks", async () => {
  const h = harness({ title: "Add undo", why: "Deleting a card is final today.", criteria: "Undo restores the last deleted card" });
  init(h, { projects: ["demo"], project: "demo" });
  const done = h.ctl.create();
  assert.equal(h.posts.length, 1);
  const send = h.posts[0];
  assert.equal(send.type, "protoComponent:send");
  assert.equal(send.nonce, NONCE);
  assert.equal(send.text, "Create board card · Add undo\nProject: demo · Priority: P2\nWhy: Deleting a card is final today.\n- Undo restores the last deleted card");
  assert.deepEqual(h.busy, [true]);
  h.ctl.create(); // a second click while the chip is open does nothing
  assert.equal(h.posts.length, 1);
  h.ctl.onMessage({ type: "protoComponent:bridgeResult", cid: send.cid, ok: true, text: send.text });
  const out = await done;
  assert.equal(out.text, send.text);
  assert.equal(h.statuses.at(-1), "Sent. The agent is creating the card.");
  assert.ok(h.isLocked() && h.ctl.sent());
  await h.ctl.create();
  assert.equal(h.posts.length, 1, "never sends twice");
});

test("a dismissed or refused proposal re-enables the button with a quiet note", async () => {
  const h = harness({ title: "t", why: "w", criteria: "c" });
  init(h, {});
  let p = h.ctl.create();
  h.ctl.onMessage({ type: "protoComponent:bridgeResult", cid: h.posts[0].cid, ok: false, error: "The user dismissed this message — it was not sent to chat." });
  await p;
  assert.equal(h.statuses.at(-1), "Not sent. Edit it and press Create card again.");
  assert.deepEqual(h.busy, [true, false]);
  assert.ok(!h.isLocked());
  p = h.ctl.create();
  h.ctl.onMessage({ type: "protoComponent:bridgeResult", cid: h.posts[1].cid, ok: false, error: "the agent is busy" });
  await p;
  assert.equal(h.statuses.at(-1), "Not sent: the agent is busy");
});

test("a bridgeResult for another cid is ignored", async () => {
  const h = harness({ title: "t", why: "w", criteria: "c" });
  init(h, {});
  const p = h.ctl.create();
  h.ctl.onMessage({ type: "protoComponent:bridgeResult", cid: 999, ok: true });
  assert.equal(h.statuses.at(-1), "Confirm it in the chat to create the card.");
  h.ctl.onMessage({ type: "protoComponent:bridgeResult", cid: h.posts[0].cid, ok: true });
  await p;
  assert.ok(h.ctl.sent());
});

test("a re-init with the same props keeps what the user typed (no re-render); no init → no send", async () => {
  const h = harness({ title: "t", why: "w", criteria: "c" });
  await h.ctl.create();
  assert.equal(h.posts.length, 0, "nothing before init");
  init(h, { projects: ["a", "b"] });
  init(h, { projects: ["a", "b"] });
  assert.equal(h.renders.length, 1);
});

test("an init without a nonce (an older core) still sends, without the field", async () => {
  const h = harness({ title: "t", why: "w", criteria: "c" });
  h.ctl.onMessage({ type: "protoComponent:init", props: {}, theme: {} });
  h.ctl.create();
  assert.equal(h.posts[0].type, "protoComponent:send");
  assert.ok(!("nonce" in h.posts[0]));
});

test("handshake posts ready once with the pair", () => {
  const posts = [];
  const win = { location: { hash: "#pair=" + "ab".repeat(16), pathname: "/p", search: "" }, history: { replaceState() {} } };
  assert.equal(BF.handshake(win, (m) => posts.push(m)), "ab".repeat(16));
  assert.equal(BF.handshake(win, (m) => posts.push(m)), null);
  assert.deepEqual(posts, [{ type: "protoComponent:ready", pair: "ab".repeat(16) }]);
  assert.equal(BF.handshake({ location: { hash: "" } }, (m) => posts.push(m)), null);
});
