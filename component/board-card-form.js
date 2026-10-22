// The new-card form in chat — the `board-card-form` frame component's page logic.
//
// A FIXED page (served by the plugin, framed by the console per ADR 0118 D5) instead of an
// HTML form the model writes on every request. The console frames it in a
// sandbox="allow-scripts" iframe on an OPAQUE origin with no bearer and NO allow-forms, so:
//   • there is no form element and no submit — the Create card button is type="button" with a click
//     handler (a sandboxed form submit is blocked and its handler never runs);
//   • the page reads nothing: its props (project names, a preselected project, placeholder
//     hints) arrive in protoComponent:init;
//   • the button PROPOSES one compact message (protoComponent:send). The host shows the
//     operator a "Send to chat" ConfirmChip and, on Send, posts it as an ordinary user turn —
//     the agent creates the card from it and can still ask a follow-up.
//
// The wire (same as component/board-cards.js):
//   handshake     the host loads us at `<frame_url>#pair=<hex>`; we post protoComponent:ready
//                 {pair} ONCE and it answers with ONE protoComponent:init {props, theme, nonce}
//   host → frame  protoComponent:init {props, theme, nonce} · protoComponent:theme {theme}
//                 protoComponent:bridgeResult {cid, ok, text?, error?}   (the send's verdict)
//   frame → host  protoComponent:send {cid, text, nonce} · protoComponent:height {height}
//
// The logic is a factory with every side effect injected, so tests drive it under node with
// no DOM: `require("component/board-card-form.js")`. In the browser the same file is inlined
// into board-card-form.html and `BoardCardForm.start()` boots it. Vanilla, no build.
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  else root.BoardCardForm = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // The first line of every proposal — the card-from-chat skill keys on it
  // (board_card_form.MESSAGE_PREFIX mirrors it).
  var PREFIX = "Create board card · ";
  var TITLE_MAX = 120;
  var WHY_MAX = 1200;
  var CRITERIA_MAX = 12;
  var CRITERION_MAX = 240;
  var PRIORITIES = [
    { value: "P0", label: "P0 · urgent" },
    { value: "P1", label: "P1 · high" },
    { value: "P2", label: "P2 · normal" },
    { value: "P3", label: "P3 · low" },
    { value: "P4", label: "P4 · someday" },
  ];
  var DEFAULT_PRIORITY = "P2";
  var PROJECT_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
  var NONCE_RE = /^[0-9a-fA-F]{32}$/;

  // ── pure helpers ────────────────────────────────────────────────────────────────

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function oneLine(s) {
    return String(s == null ? "" : s).replace(/\s+/g, " ").trim();
  }

  /** The criteria textarea → one criterion per non-empty line, list markers stripped
   *  ("- x", "* x", "• x", "1. x", "2) x", "[ ] x"). */
  function parseCriteria(text) {
    return String(text == null ? "" : text)
      .split(/\r?\n/)
      .map(function (l) {
        return oneLine(l.replace(/^\s*(?:[-*•]|\d+[.)]|\[[ xX]?\])\s*/, ""));
      })
      .filter(Boolean);
  }

  /** The props the page trusts: project names that look like names, a selected project
   *  that is one of them (else the first), and one-line hints. */
  function normalizeProps(props) {
    props = props && typeof props === "object" ? props : {};
    var projects = Array.isArray(props.projects)
      ? props.projects.filter(function (p, i, a) { return typeof p === "string" && PROJECT_RE.test(p) && a.indexOf(p) === i; })
      : [];
    var project = typeof props.project === "string" && PROJECT_RE.test(props.project) ? props.project : "";
    if (projects.length && projects.indexOf(project) < 0) project = projects[0];
    if (!projects.length && project) projects = [project];
    return {
      projects: projects,
      project: project,
      showPicker: projects.length > 1,
      titleHint: oneLine(props.title_hint).slice(0, 160),
      whyHint: oneLine(props.why_hint).slice(0, 160),
    };
  }

  /** What's wrong with the values, field by field ({} when none). */
  function validate(v) {
    var bad = {};
    if (!v.title) bad.title = "Add a title.";
    else if (v.title.length > TITLE_MAX) bad.title = "Keep the title under " + TITLE_MAX + " characters.";
    if (!v.why) bad.why = "Say what should change and why.";
    else if (v.why.length > WHY_MAX) bad.why = "Keep this under " + WHY_MAX + " characters.";
    if (!v.criteria.length) bad.criteria = "Add at least one criterion.";
    else if (v.criteria.length > CRITERIA_MAX) bad.criteria = "At most " + CRITERIA_MAX + " criteria.";
    else if (v.criteria.some(function (c) { return c.length > CRITERION_MAX; })) {
      bad.criteria = "Keep each criterion under " + CRITERION_MAX + " characters.";
    }
    return bad;
  }

  /** The proposal. Compact (the confirm chip shows it whole) but machine-readable:
   *    Create board card · <title>
   *    Project: <name> · Priority: P<n>
   *    Why: <what and why, one line>
   *    - <criterion>
   *    - <criterion> */
  function buildMessage(v) {
    var meta = [];
    if (v.project) meta.push("Project: " + v.project);
    meta.push("Priority: " + (v.priority || DEFAULT_PRIORITY));
    var lines = [PREFIX + oneLine(v.title), meta.join(" · "), "Why: " + oneLine(v.why)];
    v.criteria.forEach(function (c) { lines.push("- " + oneLine(c)); });
    return lines.join("\n");
  }

  /** Raw field values → the values the message is built from. */
  function collect(raw, props) {
    var p = normalizeProps(props);
    var project = oneLine(raw.project);
    if (p.projects.length && p.projects.indexOf(project) < 0) project = p.project;
    var priority = /^P[0-4]$/.test(raw.priority || "") ? raw.priority : DEFAULT_PRIORITY;
    return {
      project: project,
      priority: priority,
      title: oneLine(raw.title),
      why: oneLine(raw.why),
      criteria: parseCriteria(raw.criteria),
    };
  }

  function field(id, label, control, opts) {
    opts = opts || {};
    var h = '<div class="cf-field" data-field="' + id + '">';
    h += '<label class="pl-field__label" for="cf-' + id + '">' + esc(label);
    if (opts.aside) h += ' <span class="cf-aside">' + esc(opts.aside) + "</span>";
    h += "</label>" + control;
    h += '<span class="cf-err" id="cf-' + id + '-err" aria-live="polite"></span></div>';
    return h;
  }

  /** The page markup for some props. Every prop-derived string is escaped. No form element. */
  function renderHTML(props) {
    var p = normalizeProps(props);
    var h = [];
    h.push('<div class="cf pl-card" role="group" aria-labelledby="cf-eyebrow">');
    var eyebrow = "New board card" + (!p.showPicker && p.project ? " · " + p.project : "");
    h.push('<div class="pl-eyebrow cf-eyebrow" id="cf-eyebrow">' + esc(eyebrow) + "</div>");
    if (p.showPicker) {
      var opts = p.projects.map(function (name) {
        return '<option value="' + esc(name) + '"' + (name === p.project ? " selected" : "") + ">" + esc(name) + "</option>";
      });
      h.push(field("project", "Project", '<select id="cf-project" class="pl-input pl-select">' + opts.join("") + "</select>"));
    }
    var titlePh = p.titleHint ? "e.g. " + p.titleHint : "e.g. Add an undo command";
    h.push(field("title", "Title", '<input id="cf-title" class="pl-input" type="text" autocomplete="off" maxlength="' + TITLE_MAX + '" placeholder="' + esc(titlePh) + '" aria-describedby="cf-title-err">'));
    var whyPh = p.whyHint || "What should change, and why it matters";
    h.push(field("why", "What & why", '<textarea id="cf-why" class="pl-input pl-textarea" rows="3" maxlength="' + WHY_MAX + '" placeholder="' + esc(whyPh) + '" aria-describedby="cf-why-err"></textarea>'));
    h.push(field("criteria", "Acceptance criteria", '<textarea id="cf-criteria" class="pl-input pl-textarea" rows="3" placeholder="One testable result per line" aria-describedby="cf-criteria-err"></textarea>', { aside: "one per line" }));
    var pri = PRIORITIES.map(function (o) {
      return '<option value="' + o.value + '"' + (o.value === DEFAULT_PRIORITY ? " selected" : "") + ">" + esc(o.label) + "</option>";
    });
    h.push(field("priority", "Priority", '<select id="cf-priority" class="pl-input pl-select">' + pri.join("") + "</select>"));
    h.push('<div class="cf-foot"><button type="button" class="pl-btn pl-btn--primary" id="cf-create">Create card</button>');
    h.push('<span class="cf-status" id="cf-status" role="status" aria-live="polite"></span></div>');
    h.push("</div>");
    return h.join("");
  }

  /** The status line for a send verdict (the host's bridgeResult). */
  function verdictStatus(res) {
    if (res && res.ok) return { sent: true, text: "Sent. The agent is creating the card." };
    var err = String((res && res.error) || "no answer");
    if (/dismissed/i.test(err)) return { sent: false, text: "Not sent. Edit it and press Create card again." };
    return { sent: false, text: "Not sent: " + err };
  }

  // ── the controller (side effects injected) ──────────────────────────────────────

  /**
   * env: { post(msg), render(html), applyTheme(theme), read() → raw values,
   *        markInvalid(badMap), setStatus(text), setBusy(bool), lock(), setTimeout? }
   */
  function createController(env) {
    var setT = env.setTimeout || setTimeout;
    var props = null;
    var propsKey = "";
    var nonce = null;
    var nextCid = 1;
    var waiter = null; // the one in-flight send: {cid, resolve}
    var sent = false;

    function onMessage(m) {
      if (!m || typeof m !== "object") return;
      if (m.type === "protoComponent:init") {
        nonce = typeof m.nonce === "string" && NONCE_RE.test(m.nonce) ? m.nonce : null;
        env.applyTheme(m.theme);
        var key = JSON.stringify(m.props || {});
        if (props && key === propsKey) return; // a re-init must not wipe what the user typed
        props = m.props && typeof m.props === "object" ? m.props : {};
        propsKey = key;
        if (!sent) env.render(renderHTML(props));
      } else if (m.type === "protoComponent:theme") {
        env.applyTheme(m.theme);
      } else if (m.type === "protoComponent:bridgeResult") {
        if (!waiter || m.cid !== waiter.cid) return;
        var w = waiter;
        waiter = null;
        w.resolve(m);
      }
    }

    function send(text) {
      return new Promise(function (resolve) {
        var cid = nextCid++;
        waiter = { cid: cid, resolve: resolve };
        // The host answers once the operator decides; a long silence frees the button.
        setT(function () {
          if (waiter && waiter.cid === cid) { waiter = null; resolve({ ok: false, error: "no answer from the console" }); }
        }, 300000);
        var msg = { type: "protoComponent:send", cid: cid, text: text };
        if (nonce) msg.nonce = nonce;
        env.post(msg);
      });
    }

    /** The Create card click: validate → propose → report the verdict. */
    function create() {
      if (waiter || sent || !props) return Promise.resolve(null);
      var v = collect(env.read(), props);
      var bad = validate(v);
      env.markInvalid(bad);
      if (Object.keys(bad).length) {
        env.setStatus("Fill in the marked fields.");
        return Promise.resolve(null);
      }
      var text = buildMessage(v);
      env.setBusy(true);
      env.setStatus("Confirm it in the chat to create the card.");
      return send(text).then(function (res) {
        var st = verdictStatus(res);
        env.setStatus(st.text);
        if (st.sent) {
          sent = true;
          env.lock();
        } else {
          env.setBusy(false);
        }
        return { text: text, result: res };
      });
    }

    return {
      onMessage: onMessage,
      create: create,
      nonce: function () { return nonce; },
      props: function () { return props; },
      sent: function () { return sent; },
    };
  }

  // The console's theme keys → the --pl-* tokens the DS kit reads; --pl-* keys pass through
  // (the same shim as board-cards.js).
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
      if (key === "color-scheme" || key === "colorScheme" || key === "mode") {
        if (v === "light" || v === "dark") rootEl.style.colorScheme = v;
        return;
      }
      if (key.indexOf("--pl-") === 0) { rootEl.style.setProperty(key, v); return; }
      (TOKEN_MAP[key] || []).forEach(function (cssVar) { rootEl.style.setProperty(cssVar, v); });
    });
  }

  function readPair(hash) {
    var m = /^#(?:.*&)?pair=([0-9a-fA-F]{16,128})(?:&|$)/.exec(String(hash || ""));
    return m ? m[1] : null;
  }

  /** The #4172 handshake: post protoComponent:ready {pair} EXACTLY ONCE (a second ready
   *  revokes the mount). No `#pair=` ⇒ a pre-handshake host: wait for its init. */
  function handshake(win, post) {
    if (win.__boardCardFormReady) return null;
    var pair = readPair(win.location && win.location.hash);
    if (!pair) return null;
    win.__boardCardFormReady = true;
    post({ type: "protoComponent:ready", pair: pair });
    try {
      win.history.replaceState(null, "", win.location.pathname + win.location.search);
    } catch (_) {
      /* sandboxed opaque origin — the fragment stays; it is only good for this one ready */
    }
    return pair;
  }

  /** Boot in the browser: wire the controller to the DOM and the host. */
  function start(win, doc) {
    win = win || window;
    doc = doc || document;
    var mount = doc.getElementById("form");
    var postUp = function (msg) {
      try { win.parent.postMessage(msg, "*"); } catch (_) { /* detached */ }
    };
    function reportHeight() {
      // The BODY's own box, not documentElement.scrollHeight: that never drops below the
      // iframe's current height, so the frame would keep the space a cleared error line took.
      postUp({ type: "protoComponent:height", height: Math.ceil(doc.body.getBoundingClientRect().height) });
    }
    function $(id) { return doc.getElementById("cf-" + id); }
    function val(id) { var el = $(id); return el ? el.value : ""; }
    var ctl = createController({
      post: postUp,
      applyTheme: function (t) { applyThemeTo(doc.documentElement, t); },
      render: function (html) { mount.innerHTML = html; reportHeight(); },
      read: function () {
        return { project: val("project"), title: val("title"), why: val("why"), criteria: val("criteria"), priority: val("priority") };
      },
      markInvalid: function (bad) {
        ["title", "why", "criteria"].forEach(function (id) {
          var el = $(id);
          var err = $(id + "-err");
          if (el) el.setAttribute("aria-invalid", bad[id] ? "true" : "false");
          if (err) err.textContent = bad[id] || "";
        });
        reportHeight();
      },
      setStatus: function (text) { var s = $("status"); if (s) s.textContent = text; reportHeight(); },
      setBusy: function (busy) { var b = $("create"); if (b) b.disabled = !!busy; },
      lock: function () {
        ["project", "title", "why", "criteria", "priority"].forEach(function (id) {
          var el = $(id);
          if (el) el.disabled = true;
        });
        var b = $("create");
        if (b) { b.disabled = true; b.textContent = "Sent"; }
      },
    });
    win.addEventListener("message", function (e) {
      if (e.source !== win.parent) return; // only our host
      ctl.onMessage(e.data);
    });
    handshake(win, postUp); // listening first, so the one init can't be missed
    mount.addEventListener("click", function (e) {
      var t = e.target && e.target.closest ? e.target.closest("#cf-create") : null;
      if (!t) return;
      e.preventDefault();
      ctl.create();
    });
    // Clear a field's error as soon as the user fixes it.
    mount.addEventListener("input", function (e) {
      var el = e.target;
      if (!el || el.getAttribute("aria-invalid") !== "true") return;
      el.setAttribute("aria-invalid", "false");
      var err = doc.getElementById(el.id + "-err");
      if (err) err.textContent = "";
      reportHeight();
    });
    if (typeof win.ResizeObserver === "function") new win.ResizeObserver(reportHeight).observe(doc.body);
    return ctl;
  }

  return {
    PREFIX: PREFIX,
    PRIORITIES: PRIORITIES,
    TITLE_MAX: TITLE_MAX,
    WHY_MAX: WHY_MAX,
    CRITERIA_MAX: CRITERIA_MAX,
    esc: esc,
    parseCriteria: parseCriteria,
    normalizeProps: normalizeProps,
    validate: validate,
    buildMessage: buildMessage,
    collect: collect,
    renderHTML: renderHTML,
    verdictStatus: verdictStatus,
    createController: createController,
    applyThemeTo: applyThemeTo,
    readPair: readPair,
    handshake: handshake,
    start: start,
  };
});
