// Board cards in chat (#522) — the `board-cards` frame component's page logic.
//
// The console frames this page (ADR 0118 D5) in a sandbox="allow-scripts" iframe on an
// OPAQUE origin with no bearer. Everything it knows arrives over postMessage:
//   handshake     the host loads us at `<frame_url>#pair=<hex>`; we post
//                 protoComponent:ready {pair} ONCE (see handshake()) and it answers with ONE
//   host → frame  protoComponent:init {props, theme, nonce}   the validated props (pointers only)
//                 + a per-mount 32-hex nonce (protoAgent#4172) every frame → host request echoes.
//                 No `#pair=` ⇒ a pre-handshake host (no ready; its init arrives anyway);
//                 an init with no nonce ⇒ an older core with no reads (fallback banner).
//                 protoComponent:theme {theme}         a live re-theme
//                 protoComponent:readResult {cid, ok, status, json | error}   (protoAgent#4143)
//                 protoComponent:bridgeResult {cid, ok, text?, error?}       (send/openLink verdict)
//   frame → host  protoComponent:read {cid, path, nonce}      a GET of one of the kind's declared reads
//                 protoComponent:send {cid, text, nonce}      a PROPOSAL — the host shows a confirm chip
//                 protoComponent:openLink {cid, url, nonce}   a link proposal (https only)
//                 protoComponent:height {height}       our content height
//
// The logic is a factory with every side effect injected (post, timers, clock, render), so
// tests drive it under node with no DOM: `require("component/board-cards.js")`. In the
// browser the same file is inlined into board-cards.html and `BoardCards.start()` boots it.
// Vanilla, no build — like view/board.js.
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  else root.BoardCards = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var REFRESH_MS = 15000; // re-read every ~15 s while visible
  // Longer than the host's own 10 s read timeout (#4172), so the host's verdict normally lands
  // first. A frame-side timeout is TRANSIENT (retry with backoff) — never "older core": that is
  // decided by the handshake alone (no `#pair=`, or an init with no nonce).
  var READ_TIMEOUT_MS = 15000;
  var READ_SPACING_MS = 260; // ≤ 4 reads/s per frame — the host's limit (#4143)
  var MAX_IN_FLIGHT = 2; // the host's in-flight cap
  var REASON_LOOKUPS = 5; // a query listing has no block reasons — fetch at most this many
  var RETRY_MS = 3000; // after a transient refusal (paused, rate-limited, timed out) — doubling
  var RETRY_MAX_MS = 60000; // …up to this
  // A real board card id (`bd-<hash>[.<n>…]`). Checked on the id the READ returned before any
  // proposal text is built from it, so a malformed id can never smuggle words into a send.
  var CARD_ID_RE = /^bd-[a-z0-9]+(\.[0-9]+)*$/;

  var STATE_LABEL = {
    backlog: "backlog",
    ready: "on deck",
    in_progress: "building",
    in_review: "in review",
    blocked: "blocked",
    done: "done",
    cancelled: "cancelled",
  };
  var STATE_BADGE = {
    backlog: "",
    ready: "pl-badge--success",
    in_progress: "pl-badge--info",
    in_review: "pl-badge--info",
    blocked: "pl-badge--error",
    done: "",
    cancelled: "",
  };

  // ── pure helpers ────────────────────────────────────────────────────────────────

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /** The card's effective state: the blocked flag wins over the lane it is blocked in. */
  function cardState(card) {
    if (!card) return "";
    if (card.blocked || card.board_state === "blocked") return "blocked";
    return card.board_state || card.state || "";
  }

  /** Merge is offered only when every gate the board knows is green, from fields the
   *  read returns: reviewed with nothing board-side in the way (`awaiting_merge`, which
   *  the board sets only when auto_merge is off and no review/hold/draft/CI blocker
   *  stands), not blocked, a PR to merge, and CI not known red. */
  function mergeGreen(card) {
    return (
      cardState(card) === "in_review" &&
      card.awaiting_merge === true &&
      card.ci_status !== "failing" &&
      !!card.pr_url
    );
  }

  /** The exact proposal texts (#522 spec table). The agent acts on these words. */
  var PROPOSALS = {
    unblock: function (id) { return "Unblock board card " + id; },
    why: function (id) { return "Why is board card " + id + " blocked?"; },
    rereview: function (id) { return "Ask Vera to re-review board card " + id; },
    merge: function (id) { return "Merge board card " + id; },
    coder: function (id) { return "Show the live coder run for board card " + id; },
    dispatch: function (id) { return "Dispatch board card " + id; },
  };

  /** The buttons a card shows — each only in its state. */
  function actionsFor(card) {
    if (!card || typeof card.id !== "string" || !CARD_ID_RE.test(card.id)) return [];
    var id = card.id;
    var st = cardState(card);
    var out = [];
    if (st === "blocked") {
      out.push({ key: "unblock", label: "Unblock", text: PROPOSALS.unblock(id) });
      out.push({ key: "why", label: "Why?", text: PROPOSALS.why(id) });
    } else if (st === "in_review") {
      out.push({ key: "rereview", label: "Re-review", text: PROPOSALS.rereview(id) });
      if (mergeGreen(card)) out.push({ key: "merge", label: "Merge", text: PROPOSALS.merge(id) });
    } else if (st === "in_progress") {
      out.push({ key: "coder", label: "Show coder", text: PROPOSALS.coder(id) });
    } else if (st === "ready" || st === "backlog") {
      out.push({ key: "dispatch", label: "Dispatch now", text: PROPOSALS.dispatch(id) });
    }
    return out;
  }

  /** The reads a refresh starts with: one per id, or one listing for a query. */
  function readPathsFor(props) {
    if (!props) return [];
    if (Array.isArray(props.ids) && props.ids.length) {
      return props.ids.map(function (id) { return "features/" + encodeURIComponent(id); });
    }
    // The host serves declared PATHS only (a `?query` is refused, #4143), so the query rides
    // in the path and the SERVER filters and caps it (so the cap counts this project's cards):
    //   no state, no project → `cards` (open work)       state → `cards/<state>`
    //   project only         → `cards/open/<project>`    both  → `cards/<state>/<project>`
    var state = props.state ? encodeURIComponent(props.state) : "";
    var project = props.project ? encodeURIComponent(props.project) : "";
    if (project) return ["cards/" + (state || "open") + "/" + project];
    return [state ? "cards/" + state : "cards"];
  }

  /** Read `#pair=<hex>` (protoAgent#4172) from a location hash, or null on a pre-handshake host. */
  function readPair(hash) {
    var m = /^#(?:.*&)?pair=([0-9a-fA-F]{16,128})(?:&|$)/.exec(String(hash || ""));
    return m ? m[1] : null;
  }

  var NONCE_RE = /^[0-9a-fA-F]{32}$/;

  // Host refusals that pass on their own (#4143): out of view, the per-frame rate limit, a
  // cancelled or dropped request. Keep what we had and try again soon — never a banner.
  var TRANSIENT_RE = /^(paused|read rate limited|too many reads in flight|read cancelled|read timed out|read failed)/;

  /** Classify a readResult: ok / unknown (404) / transient / board error (the route's
   *  non-2xx detail) / refused (the host said no). A frame-side timeout is transient. */
  function classify(res) {
    if (!res) return { kind: "refused", error: "no answer" };
    if (res.ok) return { kind: "ok", json: res.json };
    if (res.timeout) return { kind: "transient", error: "read timed out" }; // slow, not absent
    var status = typeof res.status === "number" ? res.status : 0;
    if (!status && TRANSIENT_RE.test(String(res.error || ""))) return { kind: "transient", error: res.error };
    if (status === 404) return { kind: "unknown" };
    var detail = res.json && typeof res.json.detail === "string" ? res.json.detail : "";
    if (status >= 400 && status < 600 && status !== 401 && status !== 403) {
      return { kind: "error", error: detail || res.error || "HTTP " + status };
    }
    return { kind: "refused", error: res.error || detail || (status ? "HTTP " + status : "refused") };
  }

  function headerLabel(props) {
    if (props && props.title) return props.title;
    var parts = [];
    if (props && props.project) parts.push("project " + props.project);
    if (props && props.state) parts.push(STATE_LABEL[props.state] || props.state);
    return parts.length ? parts.join(" · ") : "Open work";
  }

  function ago(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return s + "s ago";
    var m = Math.round(s / 60);
    return m < 60 ? m + "m ago" : Math.round(m / 60) + "h ago";
  }

  function refreshedNote(lastRefreshed, now) {
    return lastRefreshed ? "refreshed " + ago(now - lastRefreshed) : "loading…";
  }

  /** The view model — pure, from props + the latest read outcome. */
  function viewModel(props, data, now) {
    data = data || {};
    var rows = data.rows || [];
    var cards = rows.filter(function (r) { return r.card; }).map(function (r) { return r.card; });
    var nOpen = cards.filter(function (c) { var s = cardState(c); return s !== "done" && s !== "cancelled"; }).length;
    var nBlocked = cards.filter(function (c) { return cardState(c) === "blocked"; }).length;
    var header = [headerLabel(props), nOpen + " open"];
    if (nBlocked) header.push(nBlocked + " blocked");
    var banner = null;
    if (data.unsupported) {
      banner = { kind: "warning", text: "Live board reads aren't available in this console — update protoAgent to see these cards' current state." };
    } else if (data.refused) {
      banner = { kind: "warning", text: "The console refused this card read: " + data.refused };
    } else if (data.error) {
      banner = { kind: "warning", text: "The board couldn't answer: " + data.error };
    }
    return {
      header: data.loaded ? header.join(" · ") : headerLabel(props),
      note: refreshedNote(data.lastRefreshed, now),
      banner: banner,
      empty: !!data.loaded && !banner && rows.length === 0,
      more: data.more || 0,
      rows: rows.map(function (r) {
        if (!r.card) return { id: r.id, status: r.status || "pending" };
        var c = r.card;
        var st = cardState(c);
        return {
          id: c.id,
          status: "ok",
          title: c.title || "",
          state: st,
          stateLabel: STATE_LABEL[st] || String(st).replace(/_/g, " "),
          badge: STATE_BADGE[st] || "",
          project: c.project || "",
          nextAction: c.next_action || "",
          blockedReason: st === "blocked" ? c.blocked_reason || "" : "",
          prUrl: /^https:\/\//.test(c.pr_url || "") ? c.pr_url : "",
          prLabel: prLabel(c.pr_url || ""),
          attempt: st === "in_progress" && Array.isArray(c.attempts) && c.attempts.length ? c.attempts[c.attempts.length - 1] : 0,
          actions: actionsFor(c),
        };
      }),
    };
  }

  function prLabel(url) {
    var m = /\/pull\/(\d+)/.exec(url || "");
    return m ? "PR #" + m[1] : "PR";
  }

  /** The page markup for a view model. Every model-derived string is escaped. */
  function renderHTML(vm) {
    var h = [];
    h.push('<div class="bc-head"><span class="bc-title">' + esc(vm.header) + "</span>");
    h.push('<span class="bc-note" data-note>' + esc(vm.note) + "</span></div>");
    if (vm.banner) h.push('<div class="pl-callout pl-callout--' + vm.banner.kind + ' bc-banner">' + esc(vm.banner.text) + "</div>");
    if (vm.empty) h.push('<div class="bc-empty">No cards match.</div>');
    vm.rows.forEach(function (r) {
      if (r.status !== "ok") {
        var why = r.status === "unknown" ? "not on this board" : r.status === "pending" ? "loading…" : "state unavailable";
        h.push('<div class="pl-card bc-card bc-card--' + esc(r.status) + '"><div class="bc-row">');
        h.push('<code class="bc-id">' + esc(r.id) + '</code><span class="bc-muted">' + esc(why) + "</span></div></div>");
        return;
      }
      h.push('<div class="pl-card bc-card" data-card="' + esc(r.id) + '"><div class="bc-row">');
      h.push('<code class="bc-id">' + esc(r.id) + "</code>");
      h.push('<span class="bc-name">' + esc(r.title) + "</span>");
      h.push('<span class="pl-badge ' + esc(r.badge) + '">' + esc(r.stateLabel) + "</span>");
      if (r.project) h.push('<span class="bc-muted">' + esc(r.project) + "</span>");
      h.push("</div>");
      if (r.blockedReason) {
        h.push('<div class="pl-callout pl-callout--warning bc-why">' + esc(r.blockedReason) + "</div>");
      } else if (r.nextAction) {
        h.push('<div class="bc-next">' + esc(r.nextAction) + "</div>");
      }
      var foot = [];
      if (r.prUrl) foot.push('<a class="pl-link" href="#" data-open="' + esc(r.prUrl) + '">' + esc(r.prLabel) + "</a>");
      if (r.attempt) foot.push('<span class="bc-muted">attempt ' + esc(r.attempt) + "</span>");
      r.actions.forEach(function (a) {
        foot.push('<button type="button" class="pl-btn pl-btn--sm" data-send="' + esc(a.text) + '">' + esc(a.label) + "</button>");
      });
      if (foot.length) h.push('<div class="bc-foot">' + foot.join("") + "</div>");
      h.push("</div>");
    });
    if (vm.more) h.push('<div class="bc-muted bc-more">… and ' + esc(vm.more) + " more</div>");
    return h.join("");
  }

  // ── the controller (side effects injected) ──────────────────────────────────────

  function createController(env) {
    var post = env.post;
    var now = env.now || Date.now;
    var setT = env.setTimeout || setTimeout;
    var clearT = env.clearTimeout || clearTimeout;
    var render = env.render || function () {};
    var applyTheme = env.applyTheme || function () {};

    var props = null;
    var propsKey = "";
    var data = { rows: [], loaded: false, lastRefreshed: 0 };
    var visible = true;
    var refreshing = false;
    var refreshTimer = null;
    var nextCid = 1;
    var pending = {}; // cid → {resolve, timer}
    var queue = []; // [{path, resolve}]
    var inFlight = 0;
    var lastSentAt = -Infinity;
    var pumpTimer = null;
    var generation = 0; // a props change invalidates an in-flight refresh
    var nonce = null; // the host's per-mount nonce from init (#4172), echoed on every request
    var unsupported = false; // decided by the handshake ONLY (an init with no nonce), never a timeout
    var transientStreak = 0; // consecutive transient refreshes → the retry backoff
    var timedOut = {}; // cid → true for reads we gave up on; a late answer means the host is alive

    function pump() {
      pumpTimer = null;
      while (queue.length && inFlight < MAX_IN_FLIGHT) {
        var wait = lastSentAt + READ_SPACING_MS - now();
        if (wait > 0) {
          pumpTimer = setT(pump, wait);
          return;
        }
        var job = queue.shift();
        sendRead(job);
      }
    }

    function sendRead(job) {
      var cid = nextCid++;
      inFlight++;
      lastSentAt = now();
      pending[cid] = {
        resolve: job.resolve,
        timer: setT(function () { settle(cid, { ok: false, timeout: true }); }, READ_TIMEOUT_MS),
      };
      post(withNonce({ type: "protoComponent:read", cid: cid, path: job.path }, nonce));
    }

    function settle(cid, res) {
      var p = pending[cid];
      if (!p) return;
      delete pending[cid];
      clearT(p.timer);
      inFlight--;
      if (res && res.timeout) timedOut[cid] = true;
      p.resolve(res);
      if (queue.length && !pumpTimer) pump();
    }

    /** Ask the host for one of our declared reads. Resolves with the readResult. */
    function read(path) {
      return new Promise(function (resolve) {
        queue.push({ path: path, resolve: resolve });
        if (!pumpTimer) pump();
      });
    }

    function schedule(ms) {
      if (refreshTimer) clearT(refreshTimer);
      refreshTimer = null;
      if (!visible || !props) return;
      refreshTimer = setT(function () { refreshTimer = null; refresh(); }, ms);
    }

    function paint() {
      render(viewModel(props, data, now()));
    }

    function refresh() {
      if (!props || refreshing || !visible || unsupported) return Promise.resolve();
      refreshing = true;
      var gen = generation;
      var ids = Array.isArray(props.ids) && props.ids.length ? props.ids : null;
      var paths = readPathsFor(props);
      var work = ids ? refreshIds(ids, paths) : refreshQuery(paths[0]);
      return work.then(function (next) {
        refreshing = false;
        if (gen !== generation) return refresh(); // props changed mid-read — start over
        if (next.transient) {
          // The host paused, rate-limited or was slow: keep the last good state and its age,
          // and come back soon — backing off while it keeps happening.
          transientStreak++;
          next.loaded = data.loaded;
          next.lastRefreshed = data.lastRefreshed;
          data = next;
          paint();
          schedule(Math.min(RETRY_MAX_MS, RETRY_MS * Math.pow(2, transientStreak - 1)));
          return;
        }
        transientStreak = 0;
        next.loaded = true;
        next.lastRefreshed = now();
        data = next;
        paint();
        schedule(REFRESH_MS);
      });
    }

    function previousCard(id) {
      var rows = data.rows || [];
      for (var i = 0; i < rows.length; i++) if (rows[i].id === id && rows[i].card) return rows[i].card;
      return null;
    }

    function refreshIds(ids, paths) {
      return Promise.all(paths.map(read)).then(function (results) {
        var next = { rows: [] };
        results.forEach(function (res, i) {
          var c = classify(res);
          if (c.kind === "ok" && c.json && typeof c.json === "object") {
            next.rows.push({ id: ids[i], card: c.json });
          } else if (c.kind === "transient") {
            next.transient = true;
            var prev = previousCard(ids[i]);
            next.rows.push(prev ? { id: ids[i], card: prev } : { id: ids[i], status: "pending" });
          } else {
            if (c.kind === "refused" && !next.refused) next.refused = c.error;
            if (c.kind === "error" && !next.error) next.error = c.error;
            next.rows.push({ id: ids[i], status: c.kind === "unknown" ? "unknown" : "unavailable" });
          }
        });
        return next;
      });
    }

    function refreshQuery(path) {
      return read(path).then(function (res) {
        var c = classify(res);
        var next = { rows: [] };
        if (c.kind !== "ok") {
          if (c.kind === "transient") return { rows: data.rows || [], more: data.more || 0, transient: true };
          if (c.kind === "error") next.error = c.error;
          else next.refused = c.error || "refused";
          return next;
        }
        var feats = (c.json && Array.isArray(c.json.features) && c.json.features) || [];
        var shown = feats.slice(0, 20);
        // `total`: how many matched on the server before its cap (it filtered by project first).
        var total = c.json && typeof c.json.total === "number" ? c.json.total : feats.length;
        next.more = Math.max(0, total - shown.length);
        next.rows = shown.map(function (f) { return { id: f.id, card: f }; });
        // A listing carries no block reason (only a single-card read has comments): look up
        // the first few blocked cards so their reason shows, within the read budget.
        var need = shown.filter(function (f) { return cardState(f) === "blocked" && !f.blocked_reason; }).slice(0, REASON_LOOKUPS);
        return Promise.all(need.map(function (f) { return read("features/" + encodeURIComponent(f.id)); })).then(function (more) {
          more.forEach(function (r, i) {
            if (r && r.ok && r.json && r.json.blocked_reason) need[i].blocked_reason = r.json.blocked_reason;
          });
          return next;
        });
      });
    }

    function setVisible(v) {
      v = !!v;
      if (v === visible) return;
      visible = v;
      if (!visible) {
        if (refreshTimer) clearT(refreshTimer);
        refreshTimer = null;
        return;
      }
      var age = now() - (data.lastRefreshed || 0);
      if (!data.lastRefreshed || age >= REFRESH_MS) refresh();
      else schedule(REFRESH_MS - age);
    }

    function onMessage(m) {
      if (!m || typeof m !== "object") return;
      if (m.type === "protoComponent:init") {
        // The mount's nonce rides on every request we make (#4172). An init WITHOUT one is an
        // older core: it serves no reads, so show the pointers with a notice and never poll.
        // (A pre-handshake host re-posts init on a schedule; a re-init is idempotent.)
        nonce = typeof m.nonce === "string" && NONCE_RE.test(m.nonce) ? m.nonce : null;
        unsupported = !nonce;
        applyTheme(m.theme);
        var key = JSON.stringify(m.props || {});
        if (props && key === propsKey) return;
        props = m.props && typeof m.props === "object" ? m.props : {};
        propsKey = key;
        generation++;
        data = { rows: (props.ids || []).map(function (id) { return { id: id, status: unsupported ? "unavailable" : "pending" }; }), loaded: unsupported, lastRefreshed: 0, unsupported: unsupported };
        paint();
        refresh(); // immediately on mount (a no-op on an older core)
      } else if (m.type === "protoComponent:theme") {
        applyTheme(m.theme);
        if (props) {
          if (refreshTimer) clearT(refreshTimer);
          refreshTimer = null;
          refresh(); // and immediately on re-theme
        }
      } else if (m.type === "protoComponent:readResult") {
        if (typeof m.cid !== "number") return;
        if (pending[m.cid]) {
          settle(m.cid, m);
        } else if (timedOut[m.cid]) {
          // A read we had given up on answered after all: the host is alive, just slow.
          // Re-read now instead of waiting out the backoff.
          delete timedOut[m.cid];
          if (m.ok && !refreshing && visible) {
            if (refreshTimer) clearT(refreshTimer);
            refreshTimer = null;
            refresh();
          }
        }
      }
    }

    return {
      onMessage: onMessage,
      refresh: refresh,
      setVisible: setVisible,
      paint: paint,
      read: read,
      nonce: function () { return nonce; },
      unsupported: function () { return unsupported; },
      state: function () {
        return { props: props, data: data, visible: visible, refreshing: refreshing, scheduled: !!refreshTimer, inFlight: inFlight, queued: queue.length };
      },
    };
  }

  /** Stamp the mount's nonce on a frame → host request (#4172). An older host that sent none
   *  gets the message without the field. */
  function withNonce(msg, nonce) {
    if (typeof nonce === "string" && nonce) msg.nonce = nonce;
    return msg;
  }

  /** The send/openLink bridge: each call is a PROPOSAL the host turns into a confirm chip.
   *  Resolves with the host's bridgeResult (or a timeout). `getNonce` returns the mount's
   *  nonce from init, echoed on every request. */
  function createBridge(post, setT, getNonce) {
    setT = setT || setTimeout;
    var nextCid = 100000; // disjoint from read cids — they share the bridgeResult/readResult space only by type
    var waiters = {};
    function call(type, field, value) {
      return new Promise(function (resolve) {
        var cid = nextCid++;
        waiters[cid] = resolve;
        setT(function () {
          if (waiters[cid]) { delete waiters[cid]; resolve({ ok: false, error: "no answer" }); }
        }, 120000);
        var msg = { type: type, cid: cid };
        msg[field] = value;
        post(withNonce(msg, getNonce ? getNonce() : null));
      });
    }
    return {
      send: function (text) { return call("protoComponent:send", "text", String(text)); },
      openLink: function (url) { return call("protoComponent:openLink", "url", String(url)); },
      onMessage: function (m) {
        if (!m || m.type !== "protoComponent:bridgeResult" || !waiters[m.cid]) return;
        var w = waiters[m.cid];
        delete waiters[m.cid];
        w(m);
      },
    };
  }

  // The console's curated theme keys → the --pl-* tokens the DS kit reads (the same map
  // the plugin-kit applies for rail views); --pl-* keys pass straight through.
  var TOKEN_MAP = {
    bg: ["--pl-color-bg"],
    bgPanel: ["--pl-color-bg-raised", "--pl-color-bg-subtle"],
    fg: ["--pl-color-fg"],
    fgMuted: ["--pl-color-fg-muted"],
    brand: ["--pl-color-accent"],
    border: ["--pl-color-border"],
  };

  function applyThemeTo(rootEl, theme) {
    if (!theme || typeof theme !== "object" || !rootEl) return;
    Object.keys(theme).forEach(function (key) {
      var v = theme[key];
      if (v == null || v === "") return;
      v = String(v);
      if (key === "color-scheme" || key === "colorScheme") {
        if (v === "light" || v === "dark") rootEl.style.colorScheme = v;
        return;
      }
      if (key === "mode") {
        if (v === "light" || v === "dark") rootEl.style.colorScheme = v;
        return;
      }
      if (key.indexOf("--pl-") === 0) { rootEl.style.setProperty(key, v); return; }
      (TOKEN_MAP[key] || []).forEach(function (cssVar) { rootEl.style.setProperty(cssVar, v); });
    });
  }

  /** The #4172 handshake. The host loads us at `<frame_url>#pair=<hex>` (fresh per mount); we
   *  post `protoComponent:ready {pair}` EXACTLY ONCE (a second ready revokes the mount) and it
   *  answers with ONE `protoComponent:init {props, theme, nonce}`. No `#pair=` ⇒ a
   *  pre-handshake host: send nothing and wait for its init the old way. The pair is then
   *  cleared from the URL (best effort — an opaque-origin frame may refuse replaceState).
   *  Returns the pair it sent, or null. Call AFTER the message listener is registered. */
  function handshake(win, post) {
    if (win.__boardCardsReady) return null; // never twice, whatever calls this
    var pair = readPair(win.location && win.location.hash);
    if (!pair) return null;
    win.__boardCardsReady = true;
    post({ type: "protoComponent:ready", pair: pair });
    try {
      var loc = win.location;
      win.history.replaceState(null, "", loc.pathname + loc.search);
    } catch (_) {
      /* sandboxed opaque origin — the fragment stays; it is only good for this one ready */
    }
    return pair;
  }

  /** Boot in the browser: wire the controller to the DOM and the host. */
  function start(win, doc) {
    win = win || window;
    doc = doc || document;
    var mount = doc.getElementById("cards");
    var postUp = function (msg) {
      try { win.parent.postMessage(msg, "*"); } catch (_) { /* detached */ }
    };
    var lastVm = null;
    function reportHeight() {
      postUp({ type: "protoComponent:height", height: Math.ceil(doc.documentElement.scrollHeight) });
    }
    var ctl = null;
    var bridge = createBridge(postUp, setTimeout, function () { return ctl ? ctl.nonce() : null; });
    ctl = createController({
      post: postUp,
      applyTheme: function (t) { applyThemeTo(doc.documentElement, t); },
      render: function (vm) {
        lastVm = vm;
        mount.innerHTML = renderHTML(vm);
        reportHeight();
      },
    });
    win.addEventListener("message", function (e) {
      if (e.source !== win.parent) return; // only our host
      ctl.onMessage(e.data);
      bridge.onMessage(e.data);
    });
    handshake(win, postUp); // listening first, so the one init can't be missed
    mount.addEventListener("click", function (e) {
      var t = e.target && e.target.closest ? e.target.closest("[data-send],[data-open]") : null;
      if (!t) return;
      e.preventDefault();
      if (t.hasAttribute("data-open")) {
        bridge.openLink(t.getAttribute("data-open"));
        return;
      }
      t.disabled = true;
      bridge.send(t.getAttribute("data-send")).then(function () { t.disabled = false; });
    });
    // Stop refreshing while scrolled out of view (or the tab is hidden).
    var inView = true;
    function sync() { ctl.setVisible(inView && doc.visibilityState !== "hidden"); }
    if (typeof win.IntersectionObserver === "function") {
      new win.IntersectionObserver(function (entries) {
        inView = entries.some(function (en) { return en.isIntersecting; });
        sync();
      }).observe(doc.body);
    }
    doc.addEventListener("visibilitychange", sync);
    // The "refreshed Ns ago" note ticks without a re-render.
    setInterval(function () {
      if (!lastVm) return;
      var note = mount.querySelector("[data-note]");
      var st = ctl.state();
      if (note && st.data.lastRefreshed) note.textContent = refreshedNote(st.data.lastRefreshed, Date.now());
    }, 5000);
    if (typeof win.ResizeObserver === "function") new win.ResizeObserver(reportHeight).observe(doc.body);
    return ctl;
  }

  return {
    REFRESH_MS: REFRESH_MS,
    READ_TIMEOUT_MS: READ_TIMEOUT_MS,
    READ_SPACING_MS: READ_SPACING_MS,
    MAX_IN_FLIGHT: MAX_IN_FLIGHT,
    PROPOSALS: PROPOSALS,
    esc: esc,
    cardState: cardState,
    mergeGreen: mergeGreen,
    actionsFor: actionsFor,
    readPathsFor: readPathsFor,
    readPair: readPair,
    CARD_ID_RE: CARD_ID_RE,
    classify: classify,
    viewModel: viewModel,
    renderHTML: renderHTML,
    createController: createController,
    createBridge: createBridge,
    withNonce: withNonce,
    applyThemeTo: applyThemeTo,
    handshake: handshake,
    start: start,
  };
});
