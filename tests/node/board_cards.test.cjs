// Page logic for the in-chat board cards (#522), under node — no DOM, no host.
// Run by tests/test_board_cards_522.py (`node --test`), or directly:
//   node --test tests/node/board_cards.test.cjs
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const BC = require(path.join(__dirname, "..", "..", "component", "board-cards.js"));

// ── a fake clock + timers + host ────────────────────────────────────────────────────

function harness() {
  let clock = 1_000_000;
  let seq = 0;
  const timers = new Map();
  const posts = [];
  const renders = [];
  const themes = [];
  const env = {
    now: () => clock,
    setTimeout: (fn, ms) => {
      const id = ++seq;
      timers.set(id, { at: clock + Math.max(0, ms), fn });
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
    post: (m) => posts.push(m),
    render: (vm) => renders.push(vm),
    applyTheme: (t) => themes.push(t),
  };
  const flush = () => new Promise((r) => setImmediate(r));
  async function advance(ms) {
    const until = clock + ms;
    for (;;) {
      const due = [...timers.entries()].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      timers.delete(due[0]);
      clock = Math.max(clock, due[1].at);
      due[1].fn();
      await flush();
    }
    clock = until;
    await flush();
  }
  const ctl = BC.createController(env);
  const reads = () => posts.filter((m) => m.type === "protoComponent:read");
  // Answer every read not yet answered, via `answer(path) → readResult fields`.
  const answered = new Set();
  async function answerAll(answer) {
    for (let round = 0; round < 50; round++) {
      const open = reads().filter((m) => !answered.has(m.cid));
      if (!open.length) {
        await advance(BC.READ_SPACING_MS); // let the pump send what it spaced out
        if (!reads().some((m) => !answered.has(m.cid))) return;
        continue;
      }
      for (const m of open) {
        answered.add(m.cid);
        ctl.onMessage({ type: "protoComponent:readResult", cid: m.cid, ...answer(m.path) });
      }
      await flush();
    }
  }
  return { env, ctl, posts, renders, themes, timers, advance, flush, reads, answerAll, now: () => clock };
}

const card = (over) => ({ id: "bd-a1", title: "A card", board_state: "ready", blocked: false, pr_url: "", ...over });

// ── buttons per state + the exact proposal texts ────────────────────────────────────

test("each state gets exactly its buttons, with the spec's exact proposal texts", () => {
  const texts = (c) => BC.actionsFor(c).map((a) => [a.label, a.text]);
  assert.deepEqual(texts(card({ id: "bd-b1", board_state: "blocked", blocked: true })), [
    ["Unblock", "Unblock board card bd-b1"],
    ["Why?", "Why is board card bd-b1 blocked?"],
  ]);
  // A card blocked IN review is blocked: the blocked buttons, never Merge.
  assert.deepEqual(texts(card({ id: "bd-b2", board_state: "in_review", blocked: true, awaiting_merge: true, pr_url: "https://github.com/o/r/pull/1" })), [
    ["Unblock", "Unblock board card bd-b2"],
    ["Why?", "Why is board card bd-b2 blocked?"],
  ]);
  assert.deepEqual(texts(card({ id: "bd-r1", board_state: "in_review" })), [["Re-review", "Ask Vera to re-review board card bd-r1"]]);
  assert.deepEqual(texts(card({ id: "bd-p1", board_state: "in_progress" })), [["Show coder", "Show the live coder run for board card bd-p1"]]);
  assert.deepEqual(texts(card({ id: "bd-d1", board_state: "ready" })), [["Dispatch now", "Dispatch board card bd-d1"]]);
  assert.deepEqual(texts(card({ id: "bd-d2", board_state: "backlog" })), [["Dispatch now", "Dispatch board card bd-d2"]]);
  assert.deepEqual(texts(card({ board_state: "done" })), []);
  assert.deepEqual(texts(card({ board_state: "cancelled" })), []);
});

test("Merge shows only when the gates the read returns are green", () => {
  const green = card({ id: "bd-m1", board_state: "in_review", awaiting_merge: true, pr_url: "https://github.com/o/r/pull/7" });
  assert.deepEqual(BC.actionsFor(green).map((a) => a.text), ["Ask Vera to re-review board card bd-m1", "Merge board card bd-m1"]);
  for (const over of [{ awaiting_merge: false }, { awaiting_merge: undefined }, { ci_status: "failing" }, { pr_url: "" }, { blocked: true }]) {
    assert.ok(!BC.actionsFor({ ...green, ...over }).some((a) => a.key === "merge"), JSON.stringify(over));
  }
});

test("reads: one per id, or the slim board for a query — never a query string (#4143)", () => {
  assert.deepEqual(BC.readPathsFor({ ids: ["bd-a1", "bd-vbyd.24"] }), ["features/bd-a1", "features/bd-vbyd.24"]);
  assert.deepEqual(BC.readPathsFor({ state: "blocked" }), ["cards"]);
  assert.deepEqual(BC.readPathsFor({ state: "in_review", project: "protoAgent" }), ["cards"]);
  // Every path is plain segments the host's read matcher accepts.
  for (const p of [...BC.readPathsFor({ ids: ["bd-a1", "bd-vbyd.24"] }), ...BC.readPathsFor({ project: "x" })]) {
    assert.ok(p.split("/").every((seg) => /^[A-Za-z0-9._~:@+=,-]+$/.test(seg)), p);
  }
});

test("a query filters the board in the frame, by state and project", () => {
  const f = card({ board_state: "blocked", project: "a" });
  assert.ok(BC.matchesQuery({ state: "blocked" }, f));
  assert.ok(BC.matchesQuery({ state: "blocked", project: "a" }, f));
  assert.ok(!BC.matchesQuery({ state: "blocked", project: "b" }, f));
  assert.ok(!BC.matchesQuery({ state: "ready" }, f));
  assert.ok(BC.matchesQuery({ project: "a" }, f));
});

// ── render ───────────────────────────────────────────────────────────────────────────

test("render: header counts, badge, next_action, block reason callout, PR link, escaped", () => {
  const props = { ids: ["bd-1", "bd-2", "bd-3", "bd-4"], title: "Epic #4076" };
  const rows = [
    { id: "bd-1", card: card({ id: "bd-1", board_state: "in_review", next_action: "awaiting complete review", pr_url: "https://github.com/o/r/pull/42" }) },
    { id: "bd-2", card: card({ id: "bd-2", board_state: "blocked", blocked: true, blocked_reason: "gate <red>" }) },
    { id: "bd-3", card: card({ id: "bd-3", board_state: "in_progress", title: "<img src=x onerror=alert(1)>", attempts: [1, 2] }) },
    { id: "bd-4", card: card({ id: "bd-4", board_state: "done" }) },
  ];
  const vm = BC.viewModel(props, { rows, loaded: true, lastRefreshed: 1000 }, 13000);
  assert.equal(vm.header, "Epic #4076 · 3 open · 1 blocked");
  assert.equal(vm.note, "refreshed 12s ago");
  const html = BC.renderHTML(vm);
  assert.match(html, /class="pl-card bc-card"/);
  assert.match(html, /pl-badge pl-badge--info">in review</);
  assert.match(html, /pl-badge pl-badge--error">blocked</);
  assert.match(html, /awaiting complete review/);
  assert.match(html, /class="pl-callout pl-callout--warning bc-why">gate &lt;red&gt;</);
  assert.match(html, /data-open="https:\/\/github.com\/o\/r\/pull\/42">PR #42</);
  assert.match(html, /attempt 2/);
  assert.ok(!html.includes("<img"), "card text is escaped");
  assert.match(html, /data-send="Unblock board card bd-2"/);
  assert.match(html, /data-send="Ask Vera to re-review board card bd-1"/);
  assert.match(html, /data-send="Show the live coder run for board card bd-3"/);
  assert.match(html, /class="pl-btn pl-btn--sm"/);
});

test("render: a non-https PR url is never a link", () => {
  const vm = BC.viewModel({ ids: ["bd-1"] }, { rows: [{ id: "bd-1", card: card({ pr_url: "javascript:alert(1)" }) }], loaded: true, lastRefreshed: 1 }, 2);
  assert.ok(!BC.renderHTML(vm).includes("data-open"));
});

// ── the controller: mount, refresh scheduling, visibility, states ────────────────────

test("mount reads every id (≤ 2 in flight, spaced) and renders the answers", async () => {
  const h = harness();
  h.ctl.onMessage({ type: "protoComponent:init", props: { ids: ["bd-1", "bd-2", "bd-3"] }, theme: { bg: "#000" } });
  await h.flush();
  assert.deepEqual(h.themes, [{ bg: "#000" }]);
  assert.equal(h.renders[0].rows[0].status, "pending", "paints a loading state at once");
  assert.equal(h.reads().length, 1, "reads are spaced (≤ 4/s)");
  await h.advance(BC.READ_SPACING_MS * 4);
  assert.equal(h.reads().length, 2, "never more than 2 reads in flight");
  assert.deepEqual(h.reads().map((m) => m.path), ["features/bd-1", "features/bd-2"]);
  assert.ok(h.reads().every((m) => typeof m.cid === "number"));
  await h.answerAll((p) => ({ ok: true, status: 200, json: card({ id: p.split("/")[1], board_state: "in_progress" }) }));
  assert.equal(h.reads().length, 3);
  const vm = h.renders.at(-1);
  assert.deepEqual(vm.rows.map((r) => [r.id, r.state]), [["bd-1", "in_progress"], ["bd-2", "in_progress"], ["bd-3", "in_progress"]]);
  assert.equal(vm.note, "refreshed 0s ago");
});

test("refreshes every ~15s, pauses out of view, and catches up on return", async () => {
  const h = harness();
  h.ctl.onMessage({ type: "protoComponent:init", props: { state: "blocked" }, theme: {} });
  const ok = () => ({ ok: true, status: 200, json: { features: [card({ id: "bd-x", board_state: "ready" })] } });
  await h.answerAll(ok);
  assert.equal(h.reads().length, 1);
  assert.equal(h.reads()[0].path, "cards");
  assert.ok(h.ctl.state().scheduled);
  await h.advance(BC.REFRESH_MS - 1000);
  assert.equal(h.reads().length, 1, "not before ~15s");
  await h.advance(1000);
  assert.equal(h.reads().length, 2, "re-read at ~15s");
  await h.answerAll(ok);
  // Out of view: nothing scheduled, nothing read.
  h.ctl.setVisible(false);
  assert.equal(h.ctl.state().scheduled, false);
  await h.advance(BC.REFRESH_MS * 4);
  assert.equal(h.reads().length, 2);
  // Back in view after > 15s: an immediate re-read.
  h.ctl.setVisible(true);
  await h.flush();
  assert.equal(h.reads().length, 3);
});

test("a re-theme re-reads immediately; a repeated init is idempotent", async () => {
  const h = harness();
  const init = { type: "protoComponent:init", props: { ids: ["bd-1"] }, theme: {} };
  h.ctl.onMessage(init);
  const ok = (p) => ({ ok: true, status: 200, json: card({ id: p.split("/")[1] }) });
  await h.answerAll(ok);
  assert.equal(h.reads().length, 1);
  h.ctl.onMessage(init); // the host re-posts init on a schedule
  await h.flush();
  assert.equal(h.reads().length, 1, "same props ⇒ no new read");
  h.ctl.onMessage({ type: "protoComponent:theme", theme: { fg: "#fff" } });
  await h.flush();
  assert.equal(h.reads().length, 2, "re-theme ⇒ immediate re-read");
  assert.deepEqual(h.themes.at(-1), { fg: "#fff" });
});

test("a 404 is an unknown card; others still render", async () => {
  const h = harness();
  h.ctl.onMessage({ type: "protoComponent:init", props: { ids: ["bd-1", "bd-gone"] }, theme: {} });
  await h.answerAll((p) => (p.endsWith("bd-gone") ? { ok: false, status: 404, error: "unknown feature" } : { ok: true, status: 200, json: card({ id: "bd-1" }) }));
  const vm = h.renders.at(-1);
  assert.deepEqual(vm.rows.map((r) => [r.id, r.status]), [["bd-1", "ok"], ["bd-gone", "unknown"]]);
  assert.equal(vm.banner, null);
  assert.match(BC.renderHTML(vm), /bd-gone<\/code><span class="bc-muted">not on this board/);
});

test("a refused read shows the refusal, not a stale or empty board", async () => {
  const h = harness();
  h.ctl.onMessage({ type: "protoComponent:init", props: { state: "blocked" }, theme: {} });
  await h.answerAll(() => ({ ok: false, status: 403, error: "path not declared in reads" }));
  const vm = h.renders.at(-1);
  assert.equal(vm.banner.kind, "warning");
  assert.match(vm.banner.text, /refused this card read: path not declared in reads/);
  assert.equal(vm.empty, false);
  assert.match(BC.renderHTML(vm), /pl-callout pl-callout--warning bc-banner/);
});

test("a board error (JSON detail) is shown as the board's answer", async () => {
  const h = harness();
  h.ctl.onMessage({ type: "protoComponent:init", props: { state: "blocked" }, theme: {} });
  await h.answerAll(() => ({ ok: false, status: 400, error: "no repo bound" }));
  assert.match(h.renders.at(-1).banner.text, /couldn't answer: no repo bound/);
});

test("an empty query says so", async () => {
  const h = harness();
  h.ctl.onMessage({ type: "protoComponent:init", props: { state: "blocked", title: "Blocked" }, theme: {} });
  await h.answerAll(() => ({ ok: true, status: 200, json: { features: [] } }));
  const vm = h.renders.at(-1);
  assert.equal(vm.empty, true);
  assert.equal(vm.header, "Blocked · 0 open");
  assert.match(BC.renderHTML(vm), /No cards match/);
});

test("a listing's blocked cards get their reason from a single-card read", async () => {
  const h = harness();
  h.ctl.onMessage({ type: "protoComponent:init", props: { state: "blocked" }, theme: {} });
  await h.answerAll((p) =>
    p === "cards"
      ? { ok: true, status: 200, json: { features: [card({ id: "bd-b", board_state: "blocked", blocked: true }), card({ id: "bd-r", board_state: "ready" })] } }
      : { ok: true, status: 200, json: card({ id: "bd-b", board_state: "blocked", blocked: true, blocked_reason: "secret missing" }) },
  );
  assert.deepEqual(h.reads().map((m) => m.path), ["cards", "features/bd-b"]);
  assert.equal(h.renders.at(-1).rows[0].blockedReason, "secret missing");
});

test("older core: no readResult ever ⇒ the fallback banner, fast, and no polling", async () => {
  const h = harness();
  h.ctl.onMessage({ type: "protoComponent:init", props: { ids: ["bd-1", "bd-2", "bd-3", "bd-4"] }, theme: {} });
  await h.flush();
  await h.advance(BC.READ_TIMEOUT_MS + BC.READ_SPACING_MS);
  const vm = h.renders.at(-1);
  assert.ok(vm.banner && /update protoAgent/.test(vm.banner.text), JSON.stringify(vm.banner));
  assert.equal(h.reads().length, 2, "the queued reads fail at once instead of timing out one by one");
  assert.deepEqual(vm.rows.map((r) => r.status), ["unavailable", "unavailable", "unavailable", "unavailable"]);
  assert.equal(h.ctl.state().scheduled, false, "nothing to poll on a host that can't read");
});

test("a transient host refusal (paused, rate-limited) keeps the last cards and retries soon", async () => {
  const h = harness();
  h.ctl.onMessage({ type: "protoComponent:init", props: { state: "ready" }, theme: {} });
  await h.answerAll(() => ({ ok: true, status: 200, json: { features: [card({ id: "bd-r", board_state: "ready" }), card({ id: "bd-x", board_state: "done" })] } }));
  assert.deepEqual(h.renders.at(-1).rows.map((r) => r.id), ["bd-r"], "filtered to the query");
  const refreshedAt = h.ctl.state().data.lastRefreshed;
  await h.advance(BC.REFRESH_MS);
  await h.answerAll(() => ({ ok: false, status: 0, error: "paused: the component is not visible" }));
  const vm = h.renders.at(-1);
  assert.equal(vm.banner, null, "no banner for a pause");
  assert.deepEqual(vm.rows.map((r) => r.id), ["bd-r"], "the last good cards stay");
  assert.equal(h.ctl.state().data.lastRefreshed, refreshedAt, "not claimed as refreshed");
  const n = h.reads().length;
  await h.advance(3000);
  assert.equal(h.reads().length, n + 1, "retried within seconds, not 15s");
});

test("a readResult for an unknown cid is ignored", async () => {
  const h = harness();
  h.ctl.onMessage({ type: "protoComponent:readResult", cid: 999, ok: true, json: {} });
  h.ctl.onMessage({ type: "protoComponent:readResult", cid: "1", ok: true, json: {} });
  assert.equal(h.renders.length, 0);
});

// ── the send / openLink bridge ───────────────────────────────────────────────────────

test("a button sends its exact text as a protoComponent:send proposal; a PR link is openLink", async () => {
  const posts = [];
  const bridge = BC.createBridge((m) => posts.push(m), () => 0);
  const p = bridge.send("Unblock board card bd-b1");
  assert.equal(posts[0].type, "protoComponent:send");
  assert.equal(posts[0].text, "Unblock board card bd-b1");
  assert.equal(typeof posts[0].cid, "number");
  bridge.onMessage({ type: "protoComponent:bridgeResult", cid: posts[0].cid, ok: true });
  assert.deepEqual(await p, { type: "protoComponent:bridgeResult", cid: posts[0].cid, ok: true });
  bridge.openLink("https://github.com/o/r/pull/1");
  assert.deepEqual(Object.keys(posts[1]).sort(), ["cid", "type", "url"]);
  assert.equal(posts[1].type, "protoComponent:openLink");
});

test("theme: curated keys map onto --pl-* tokens; --pl-* pass through; mode sets color-scheme", () => {
  const set = {};
  const el = { style: { setProperty: (k, v) => (set[k] = v), colorScheme: "" } };
  BC.applyThemeTo(el, { bg: "#111", brand: "#a0f", "--pl-space-2": "6px", "color-scheme": "light", junk: "x", fg: "" });
  assert.deepEqual(set, { "--pl-color-bg": "#111", "--pl-color-accent": "#a0f", "--pl-space-2": "6px" });
  assert.equal(el.style.colorScheme, "light");
});
