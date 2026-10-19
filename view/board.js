// The DS plugin-kit owns the protoagent:init handshake (bearer + theme, incl. live
// re-themes onto the --pl-* tokens) and slug-aware authed fetches — replacing the
// hand-rolled TMAP/listener this page carried. plugin-kit.js is an ES MODULE, so it
// loads via dynamic import (a classic <script src> throws on its exports; see
// protoAgent docs/how-to/build-a-plugin-view.md). Older host without /_ds: fall
// back to a tokenless same-origin shim.
let kit;
try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
catch (e) { kit = { initPluginView(){}, apiFetch: (p, i) => fetch(BASE + p, i) }; }

const COLS = ["backlog", "ready", "in_progress", "in_review", "done"];
// State → operator-facing label (#182): a view-layer relabel only — internal
// state names (store/API/tools) are unchanged. `ready` means spec complete and
// queued for a coder, which read as finished under the raw name. Unknown
// states fall back to state.replace("_"," ").
const STATE_LABEL = {
  backlog: "backlog",
  ready: "on deck",
  in_progress: "building",
  in_review: "in review",
  done: "done",
  blocked: "blocked",
  cancelled: "cancelled",
};
// Slug-aware authed fetch via the kit (rules 2+3) — pass a bare /api/... path.
// Errors become READABLE: a JSON error body surfaces its `detail` (the actionable
// BoardError message), a non-JSON body its HTTP status — never a raw parse error.
const api = async (p) => {
  const r = await kit.apiFetch(p);
  const d = await r.json().catch(() => { throw new Error("HTTP " + r.status + " (non-JSON response)"); });
  if (!r.ok) throw new Error(d.detail || "HTTP " + r.status);
  return d;
};
// The board's FIRST mutation path (#217): the task-type review lane POSTs a
// deliverable / a verify verdict. Same slug-aware authed fetch as `api`, but sends a
// JSON body and decodes the same readable-error shape (a BoardError `detail`, else the
// HTTP status). Coding features never mutate from here — their lifecycle is the coder
// + the PR; only task cards call this.
const apiPost = async (p, body) => {
  const r = await kit.apiFetch(p, {method: "POST", headers: {"content-type": "application/json"},
    body: JSON.stringify(body || {})});
  const d = await r.json().catch(() => { throw new Error("HTTP " + r.status + " (non-JSON response)"); });
  if (!r.ok) throw new Error(d.detail || "HTTP " + r.status);
  return d;
};
const $ = (id) => document.getElementById(id);
const esc = (s) => (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// Narrow viewport → the 5-column Kanban doesn't fit, so default to the list.
const NARROW = window.matchMedia("(max-width: 760px)");
let VIEW = NARROW.matches ? "list" : "kanban";
function setView(v){ VIEW=v; $("tk").classList.toggle("pl-tab--active",v==="kanban"); $("tl").classList.toggle("pl-tab--active",v==="list");
  $("kanban").classList.toggle("hide",v!=="kanban"); $("list").classList.toggle("hide",v!=="list"); render(); }
// Auto-switch to list when the viewport narrows (e.g. rotating a phone / resizing).
NARROW.addEventListener("change", (e) => { if (e.matches && VIEW !== "list") setView("list"); });

let FEATURES = [];
// The next-action chip (#208, #305): `next_action` is the one sentence that says what
// moves the card. "awaiting merge" is the in_review case nobody used to be told about —
// reviewed, green, and the loop will NOT merge (auto_merge off) — so it gets the success
// chip with the how-to-move-it hint as its tooltip. "awaiting deliverable" is the parked
// task's sibling (#305): an in_progress task the loop left for an out-of-band delivery,
// its board_deliver hint riding the tooltip. The other sub-states render as neutral
// chips (the text is the server's, esc()'d).
const NEXT_ACTION_CHIP = {
  "awaiting-merge (auto_merge off)": ["pl-badge--success", "awaiting merge"],
  "auto-merge pending": ["pl-badge--info", "auto-merge pending"],
  "review in progress": ["pl-badge--info", "review in progress"],
  "changes requested": ["pl-badge--warning", "changes requested"],
  // #347: a coding card the review gate bounced is requeued out of in_review to be
  // re-driven — its own chip so an active fix round reads explicitly, not as `-`.
  "fixing review findings": ["pl-badge--warning", "fixing review"],
  "awaiting review verdict (no review-clean)": ["pl-badge--warning", "awaiting review verdict"],
  "merge-hold (operator veto)": ["", "merge-hold"],
  "draft (run `gh pr ready`)": ["pl-badge--warning", "draft"],
  "ci failing": ["pl-badge--error", "ci failing"],
  // ADR 0326: the auto-merge edge is stuck because the merged-verify re-verify budget
  // is spent while base keeps moving — a warning chip whose tooltip (the server hint)
  // names the reset / raise-cap / wait remedies, so the operator sees it on the card
  // instead of only in the loop log.
  "auto-merge held: merged-verify budget exhausted": ["pl-badge--warning", "merge held (verify budget)"],
  "awaiting deliverable": ["pl-badge--info", "awaiting deliverable"],
  // #406: a card outside the ready lane whose every dependency has closed — nothing
  // re-checks it, so the chip (and its board_mark_ready / unblock hint) is the only tell.
  "dependencies closed — promote": ["pl-badge--warning", "deps closed: promote"],
  "blocked — dependencies closed": ["pl-badge--warning", "deps closed"],
};
// Prefix-keyed chips: the sentence carries live evidence (which package, which release
// branch), so it can't be a fixed key. The full sentence rides the tooltip.
//  - publish gates (`waits_for`): a card waiting on an external publish/release/merge.
//  - release freeze: the auto-merge edge held a PR because its repo is mid-release.
const NEXT_ACTION_PREFIX_CHIP = [
  ["waiting on publish", ["pl-badge--warning", "waiting on publish"]],
  ["held: release freeze", ["pl-badge--warning", "held: release freeze"]],
];
function nextActionChip(f){
  if (!f.next_action || f.next_action === "blocked") return "";  // blocked has its own chip
  const byPrefix = NEXT_ACTION_PREFIX_CHIP.find(([p]) => f.next_action.startsWith(p));
  if (byPrefix) {
    const [cls, label] = byPrefix[1];
    const tip = f.next_action + (f.next_action_hint ? " — " + f.next_action_hint : "");
    return '<span class="pl-badge '+cls+'" title="'+esc(tip)+'">'+esc(label)+'</span>';
  }
  const [cls, label] = NEXT_ACTION_CHIP[f.next_action] || ["", f.next_action];
  const hint = f.next_action_hint || f.next_action;
  return '<span class="pl-badge '+cls+'" title="'+esc(hint)+'">'+esc(label)+'</span>';
}
function flags(f){
  let out = "";
  if (f.blocked) out += '<span class="pl-badge pl-badge--error">blocked</span>';
  if (f.dag_blocked) out += '<span class="pl-badge pl-badge--warning">waiting on deps</span>';
  out += nextActionChip(f);
  if (f.difficulty) out += '<span class="pl-badge">'+esc(f.difficulty)+'</span>';
  out += selfVerifiedBadge(f);
  return out;
}
// Render-side half of the external-ref gate (the store's normalize_external_ref is the
// persistence half), sharing sanitizeSaying()'s scheme discipline: a footer link only
// goes live when its ref parses as an absolute http(s) URL — anything else renders as
// the inert "#", so no other scheme ever reaches an href.
function safeHref(u){
  try {
    const p = new URL(String(u));
    if (p.protocol === "http:" || p.protocol === "https:") return p.href;
  } catch (e) { /* not an absolute URL → inert */ }
  return "#";
}
function pr(f){ return f.pr_url ? '<a class="pr" href="'+esc(safeHref(f.pr_url))+'" target="_blank" rel="noopener noreferrer">PR ↗</a>' : ""; }

// ── Task-type features (#217) ───────────────────────────────────────────────────
// A task rides the SAME board lanes / priority ordering / dependency + blocked display
// as a coding feature, but ships a DELIVERABLE (a doc, a decision, an artifact ref)
// instead of a coder PR. So it wears a document icon, its in_progress card carries a
// submit form, its in_review card the deliverable text + Approve/Reject, and it NEVER
// opens the coder monitor (no coder dispatch). `issue_type === "task"` is the only tell.
const TASK_TYPE = "task";
const isTask = (f) => f.issue_type === TASK_TYPE;
const FEAT = "/api/plugins/project_board/features/";
// Inline document glyph — sets a task card apart from a coding feature card.
const DOC_ICON = '<svg class="doc-ico" width="12" height="12" viewBox="0 0 16 16" fill="none"'
  + ' stroke="currentColor" stroke-width="1.3" aria-hidden="true">'
  + '<path d="M4 1.75h5L12.5 5.25V13.5a.75.75 0 0 1-.75.75h-7a.75.75 0 0 1-.75-.75z"/>'
  + '<path d="M8.75 1.75V5.5h3.75"/><path d="M6 8.5h4M6 11h4"/></svg>';
function docIco(f){ return isTask(f) ? '<span class="doc-ico-w">'+DOC_ICON+'</span>' : ""; }
// A task's `ref` (doc URL / artifact path) lands on external_ref, which the projection
// surfaces as `pr_url` — the same slot a coding feature's PR occupies. Rendered as a
// plain "ref ↗" link, distinct from a coding feature's "PR ↗".
function taskRef(f){ return f.pr_url ? '<a class="pr" href="'+esc(safeHref(f.pr_url))+'" target="_blank" rel="noopener noreferrer">ref ↗</a>' : ""; }
// A card's footer link: a task shows its external ref, a coding feature its PR.
function taskFoot(f){ return isTask(f) ? taskRef(f) : pr(f); }

// ── Self-verified task deliverables (#316 S4) ────────────────────────────────────
// S3a (#316) projects `self_verified` / `delivered_by` / `verified_by` onto every feature.
// A DONE TASK whose deliverer approved their own Done edge — the same identity delivered
// AND verified it, with no independent reviewer — carries `self_verified: true`. Surface
// that with the EXISTING caution badge flags() already uses for "waiting on deps" (no new
// visual language), gated to done tasks ONLY: a coding feature (not a task), a non-done
// task (wrong state), and an independently-verified task (`self_verified: false`) all fall
// through to "". The drawer adds a provenance line naming the deliverer + the self-
// verifying actor, every actor field esc()'d (server-authored free text — the same #101
// reason `delivered_by` rides a comment, not a label).
function isSelfVerified(f){ return isTask(f) && f.state === "done" && !!f.self_verified; }
function selfVerifiedBadge(f){
  return isSelfVerified(f) ? '<span class="pl-badge pl-badge--warning">self-verified</span>' : "";
}
// The drawer's self-verification provenance: the badge + a line naming the deliverer
// (`delivered_by`) and the self-verifying actor (`verified_by`, which on a self-verified
// edge IS the deliverer, so fall back to `delivered_by`, then to the em-dash the drawer
// uses elsewhere for an absent field). Both actor fields esc()'d — never raw HTML.
function selfVerifiedProvenance(f){
  if (!isSelfVerified(f)) return "";
  const deliverer = esc(f.delivered_by || "—");
  const verifier = esc(f.verified_by || f.delivered_by || "—");
  return '<div class="tdlbl">verification</div>'
    + '<div class="tdsec">'+selfVerifiedBadge(f)
    + ' delivered by '+deliverer+', self-verified by '+verifier+'</div>';
}

// Which in_review task cards have their reject-feedback form expanded — module-scoped
// so the 10s auto-reload re-render keeps it open (same pattern as COLLAPSED/DONE_ALL).
const REJECT_OPEN = new Set();
function toggleReject(fid){ REJECT_OPEN.has(fid) ? REJECT_OPEN.delete(fid) : REJECT_OPEN.add(fid); render(); }

// The task's action block — the board's first mutation UI. Lives in the shared
// #drawer (bd-rdmh), appended below the spec / acceptance-criteria / deliverable that
// taskDetail() renders (it no longer rides an inline card).
//   in_progress → a submit-deliverable form (text + optional ref URL).
//   in_review   → Approve / Reject (Reject expands a feedback textarea whose contents
//                 re-dispatch to the assignee); the deliverable itself is shown by
//                 taskDetail above these controls.
// Every state with an action carries its OWN error slot (terr-<id>); for in_review it
// is emitted OUTSIDE the reject-form branch so the Approve path (reject form collapsed)
// can still surface a failed verify — the review-flagged bug.
function taskExtra(f){
  const id = esc(f.id);
  if (f.state === "in_progress"){
    return '<div class="tform tact">'
      + '<textarea class="tarea" id="tdtext-'+id+'" placeholder="Deliverable — a doc, a decision, an artifact ref"></textarea>'
      + '<input class="tin" id="tdref-'+id+'" type="url" placeholder="Reference URL (optional)">'
      + '<div class="trow"><button class="tbtn tbtn--primary" data-deliver="'+id+'">Submit deliverable</button></div>'
      + '<div class="terr" id="terr-'+id+'"></div></div>';
  }
  if (f.state === "in_review"){
    let acts = '<div class="tact">'
      + '<button class="tbtn tbtn--primary" data-approve="'+id+'">Approve</button>'
      + '<button class="tbtn tbtn--danger" data-reject-toggle="'+id+'">Reject</button></div>';
    if (REJECT_OPEN.has(f.id)){
      acts += '<div class="tform tact">'
        + '<textarea class="tarea" id="trtext-'+id+'" placeholder="Feedback — what needs to change (re-dispatched to the assignee)"></textarea>'
        + '<div class="trow"><button class="tbtn tbtn--danger" data-reject="'+id+'">Send rejection</button>'
        + '<button class="tbtn" data-reject-toggle="'+id+'">Cancel</button></div></div>';
    }
    return acts + '<div class="terr" id="terr-'+id+'"></div>';
  }
  return "";
}
// The task's requirement ledger (#399) — the acceptance criteria decomposed into items a
// delivery's `## Requirements` section closes. The human verifier is who the ledger is
// surfaced FOR, so it sits right above Approve/Reject: one row per item with its status
// chip, and an OPEN row reads at full strength beside a warning chip while done/declined
// rows recede. A cue, never a gate — Approve still works with items open. Every field
// esc()'d (server-authored text).
function taskRequirements(f){
  const reqs = f.requirements || [];
  if (!reqs.length) return "";
  let open = 0, rows = "";
  for (const r of reqs){
    const st = String(r.status || "open").toLowerCase();
    const closed = st === "done" || st === "declined";
    if (!closed) open++;
    const chip = closed ? (st === "done" ? " pl-badge--success" : "") : " pl-badge--warning";
    rows += '<li'+(closed ? '' : ' class="treq--open"')+'><span class="pl-badge'+chip+'">'+esc(st)+'</span>'
      + '<span class="id">'+esc(String(r.id || ""))+'</span> '+esc(String(r.text || ""))
      + (r.decline_reason ? ' <span class="why">— '+esc(String(r.decline_reason))+'</span>' : '')
      + (r.reopened_from ? ' <span class="why">— reopened by a rejection (was '+esc(String(r.reopened_from))+')</span>' : '')+'</li>';
  }
  return '<div class="tdlbl">requirements'+(open ? ' — '+open+' open' : '')+'</div><ul class="treq">'+rows+'</ul>';
}
// The task-detail drawer body (bd-rdmh): a task's detail + controls live in the SHARED
// #drawer instead of inline on the Kanban card / list row. Renders the spec, the
// acceptance criteria, any recorded deliverable, the requirement ledger, then the
// state-appropriate controls (taskExtra). Everything is esc()'d — server-authored text,
// never raw HTML.
function taskDetail(f){
  const prose = (s) => s ? '<div class="tdsec">'+esc(s)+'</div>' : '<div class="tdsec tdsec--empty">—</div>';
  let h = '<div class="tdlbl">spec</div>' + prose(f.spec)
    + '<div class="tdlbl">acceptance criteria</div>' + prose(f.acceptance_criteria);
  if (f.deliverable) h += '<div class="tdlbl">deliverable</div><div class="deliv">'+esc(f.deliverable)+'</div>';
  h += taskRequirements(f);
  h += selfVerifiedProvenance(f);
  return h + taskExtra(f);
}
// Surface a task-action failure in the card's always-present error slot.
function taskErr(fid, e){
  const el = $("terr-"+fid);
  if (el) el.textContent = "" + ((e && e.message) || e);
}
async function submitDeliver(fid){
  const text = ($("tdtext-"+fid) || {}).value || "";
  const ref = ($("tdref-"+fid) || {}).value || "";
  // …then re-fetch the single-card detail so the drawer picks up the just-recorded
  // deliverable (the list reload above can't — /features carries only a preview; #312/#399).
  try { await apiPost(FEAT+encodeURIComponent(fid)+"/deliver", {text: text, ref: ref}); await load(); await fetchTaskDetail(fid); }
  catch (e) { taskErr(fid, e); }
}
// An approval that went through with requirements still open (#399): the verify response
// carries a `note` ("2 requirement(s) still open: r2, r4"). The approval STANDS — the human
// decided — but they are shown what they approved past, in the drawer, until it closes or
// switches task. Module-scoped so the reload + detail re-fetch right after Approve (and
// every 10s poll re-render) keeps it. Shape: {fid, note}; null when there is nothing to say.
let VERIFY_NOTE = null;
async function approveTask(fid){
  try {
    const r = await apiPost(FEAT+encodeURIComponent(fid)+"/verify", {approved: true});
    VERIFY_NOTE = r && r.note ? {fid: fid, note: String(r.note)} : null;
    await load(); await fetchTaskDetail(fid);
  }
  catch (e) { taskErr(fid, e); }
}
async function rejectTask(fid){
  const feedback = ($("trtext-"+fid) || {}).value || "";
  try {
    await apiPost(FEAT+encodeURIComponent(fid)+"/verify", {approved: false, feedback: feedback});
    REJECT_OPEN.delete(fid); await load(); await fetchTaskDetail(fid);
  } catch (e) { taskErr(fid, e); }
}

// State → DS dot variant (for the list view chip).
const DOT_VARIANT = {ready:"pl-dot--success", in_review:"pl-dot--info", blocked:"pl-dot--error"};

// List view sections, rendered as collapsible groups in this order. `blocked` comes
// FIRST, before every Kanban column: a blocked card is the board's loudest "needs
// attention" signal and the one thing an operator must not scroll to find. It used to
// sit second-to-last, BELOW done — so `list_features` floating blocked rows to the top
// (#201) had no visible effect here, because the list groups by state before it renders
// and the group order won. `cancelled` stays last: it is terminal and uninteresting.
const LIST_SECTIONS = ["blocked", ...COLS, "cancelled"];
// States the user has collapsed — module-scoped so the 10s auto-reload re-render
// doesn't re-expand what they closed.
const COLLAPSED = new Set();
function toggleGroup(state){ COLLAPSED.has(state) ? COLLAPSED.delete(state) : COLLAPSED.add(state); render(); }

// Done cap (#115): the Done column/group shows only the most recent DONE_CAP
// features (closed_at desc) with a "show all" affordance — recent merges stay on
// top instead of drowning in history. (The server already keeps archived features
// out of the default /features response; this cap covers the still-live window.)
// DONE_ALL is module-scoped so the 10s auto-reload doesn't re-truncate an expanded view.
const DONE_CAP = 20;
let DONE_ALL = false;
function showAllDone(){ DONE_ALL = true; render(); }
// → {items, total}: most recent first, capped until "show all"; total drives the
// header count badge + the affordance label so the cap is never mistaken for the count.
function doneSlice(items){
  const sorted = items.slice().sort((a,b) => (b.closed_at||"").localeCompare(a.closed_at||""));
  return {items: (DONE_ALL || sorted.length <= DONE_CAP) ? sorted : sorted.slice(0, DONE_CAP),
          total: sorted.length};
}
function showAllBtn(total){
  return '<button class="showall" onclick="showAllDone()">show all ('+total+')</button>';
}

function render(){
  // A drag owns the board while it runs — a poll that lands mid-drag is painted after it.
  if (DRAG){ RENDER_DEFERRED = true; return; }
  if (VIEW === "kanban"){
    renderKanban();
  } else {
    // List: group rows under a collapsible per-state header (COLS order + blocked +
    // cancelled), mirroring the Kanban's grouping so a dense board stays scannable (#26).
    const row = (f) => {
      // in_progress CODING rows open the monitor; a task row opens its detail drawer
      // (bd-rdmh) via a data-task handle — no inline sub-row, no monitor poll (#84/#217).
      const mon = f.state==="in_progress" && !isTask(f);
      return '<tr'+(mon?' data-mon="'+esc(f.id)+'"':"")+(isTask(f)?' data-task="'+esc(f.id)+'"':"")+'>'
        + '<td class="id">'+esc(f.id)+'</td><td>'+docIco(f)+esc(f.title)+'</td>'
        + '<td><span class="pl-dot-row"><span class="pl-dot '+(DOT_VARIANT[f.state]||"")+'"></span>'
        + '<span class="pl-dot-row__label">'+esc(STATE_LABEL[f.state] || f.state)+'</span></span></td>'
        + '<td>P'+f.priority+'</td><td>'+flags(f)+'</td><td>'+taskFoot(f)+'</td></tr>';
    };
    const byState = {};
    FEATURES.forEach(f => (byState[f.state] = byState[f.state] || []).push(f));
    // COLS order + blocked + cancelled; any unexpected state lands in its own group last.
    const order = LIST_SECTIONS.slice();
    Object.keys(byState).forEach(s => { if (!order.includes(s)) order.push(s); });
    let html = "";
    order.forEach(state => {
      let items = byState[state] || [], total = items.length;
      if (!items.length) return;  // omit empty sections
      if (state === "done"){ const d = doneSlice(items); items = d.items; total = d.total; }  // cap Done (#115)
      const collapsed = COLLAPSED.has(state);
      html += '<tr class="grp" data-state="'+esc(state)+'" onclick="toggleGroup(this.dataset.state)">'
        + '<td colspan="6"><span class="tw">'+(collapsed?"▸":"▾")+'</span>'
        + '<span class="gl">'+esc(STATE_LABEL[state] || state.replace("_"," "))+'</span>'
        + '<span class="pl-badge">'+total+'</span></td></tr>';
      if (!collapsed){  // collapsed → header only (rows omitted)
        html += items.map(row).join("");
        if (total > items.length) html += '<tr><td colspan="6">'+showAllBtn(total)+'</td></tr>';
      }
    });
    $("rows").innerHTML = html || '<tr><td colspan="6"><div class="pl-empty">No features yet — create some via the board tools or API.</div></td></tr>';
  }
  // Keep an open task drawer in sync with the freshly-rendered FEATURES (bd-rdmh): a
  // reject-toggle, a mutation's reload, or the 10s auto-reload re-renders the board, so
  // the drawer re-renders too — its state (in_progress → in_review → done) and the
  // reject-form toggle survive a re-render, the same way REJECT_OPEN/COLLAPSED do.
  syncTaskDrawer();
}

// Every full read carries the listing's ETag back as If-None-Match, so the periodic
// refresh of an unchanged board is an empty 304 — nothing to parse, nothing to paint.
// LOAD_SEQ drops a slow response that a newer read already overtook.
let FEATURES_ETAG = "", LAST_FULL = 0, LOAD_SEQ = 0;
async function load(){
  const seq = ++LOAD_SEQ;
  LAST_FULL = Date.now();
  try {
    const res = await kit.apiFetch("/api/plugins/project_board/features",
      FEATURES_ETAG ? {headers: {"if-none-match": FEATURES_ETAG}} : undefined);
    if (seq !== LOAD_SEQ) return;
    if (res.status !== 304){
      const r = await res.json().catch(() => { throw new Error("HTTP " + res.status + " (non-JSON response)"); });
      if (!res.ok) throw new Error(r.detail || "HTTP " + res.status);
      if (seq !== LOAD_SEQ) return;
      FEATURES_ETAG = res.headers.get("etag") || "";
      // the /features API field is `board_state`; normalize to `state` for the views.
      // blocked floats first (#201), in_progress second (#223) — matching list_features —
      // then priority asc, id tiebreak (sortFeatures). A move still in flight keeps its
      // optimistic patch over the fresh rows (withPending).
      FEATURES = withPending(sortFeatures((r.features || []).map(f => ({...f, state: f.board_state ?? f.state}))));
      render();
    }
    $("sub").textContent = "project_board — " + FEATURES.length + " features · a projection over beads";
    // The board READS fine but may still be unable to RUN (no coder, no gh, br
    // vanished): the setup preflight on /status says which check fails and how
    // to fix it — a warning card above the board, never a silent green.
    const s = await api("/api/plugins/project_board/status").catch(() => null);
    // br fetched on first run (v0.43.0): say so in the subtitle, once it's the store's binary.
    // Only the version on the page (it is on camera, and the path is the operator's home
    // dir); the path rides the tooltip for whoever needs it.
    if (s && s.setup && s.setup.br && s.setup.br.source === "fetched") {
      $("sub").textContent += " · br v" + (s.setup.br.fetch.version || "?");
      $("sub").title = "br fetched to " + (s.setup.br.path || "?");
    }
    // Gate-preflight holds (#255/#261): a fully-`ready` board can still pick up nothing
    // because a project's gate failed on its clean base and the loop held that project's
    // cards — without a card the board just looks idle. When setup ALSO fails, the held
    // list rides along on the setup-gap card instead of fighting it for the slot.
    if (s && s.setup && s.setup.ready === false) renderSetupGaps(s.setup, null, s);
    else if (s && s.held_projects && s.held_projects.length) renderHeldProjects(s);
    else if (s && s.setup && (s.setup.loop_cfg_stale || s.setup.db_override_ignored || s.setup.legacy_store_hint)) renderLoopStale(s.setup);
    else $("err").hidden = true;
  } catch (e) {
    // First-run tell (#unbound): a board never bound to a repo (shipped default
    // repo "." + no db_path) fails every read — that is missing SETUP, not an
    // error. /status is a pure config read, so it answers even when the store
    // can't; if it can't either, fall through to the raw error. A BOUND board
    // whose preflight fails (br missing, …) gets the same setup-gap card in
    // place of the raw BoardError: the card names the failing check + its hint.
    try {
      const s = await api("/api/plugins/project_board/status");
      if (s && s.bound === false) { renderSetup(e, s.setup); return; }
      if (s && s.setup && s.setup.ready === false) { renderSetupGaps(s.setup, e, s); return; }
    } catch (_ignored) {}
    $("err").hidden = false; $("err").className = "pl-callout pl-callout--error";
    $("err").textContent = "Could not load the board: " + e;
  }
}

// Setup preflight checks (v0.42.0), in render order, with their operator labels.
// The /status `setup` block carries one {ok, hint, …} per key; only failing ones
// render. Hints are server-authored operator copy, esc()'d — never raw HTML.
const SETUP_CHECKS = [["br", "beads CLI (br)"], ["gh", "GitHub CLI (gh)"], ["coder", "coder delegate"], ["repo", "repo"]];
function setupGapItems(setup){
  let html = "";
  for (const [key, label] of SETUP_CHECKS) {
    const c = setup && setup[key];
    if (!c || c.ok !== false) continue;
    html += '<li><b>' + esc(label) + '</b> — ' + esc(String(c.hint || (key + " check failed"))) + '</li>';
  }
  return html;
}
// Only the checks the loop refuses to tick without make the board "unable to run";
// a gh-only gap means builds can't open/merge PRs, not that the board is down.
function setupBlocking(setup){
  return SETUP_CHECKS.some(([key]) => key !== "gh" && setup && setup[key] && setup[key].ok === false);
}
function setupLoopLine(setup){
  const blockers = (setup && setup.loop_blockers) || [];
  let html = "";
  if (!setup || !setup.loop_enabled) html += '<div style="opacity:.65;margin-top:6px;font-size:12px">The build loop is off (<code>loop_enabled: false</code>).</div>';
  else if (blockers.length) html += '<div style="margin-top:6px;font-size:12px">The build loop is <b>paused</b> on: ' + esc(blockers.join(", ")) + ' — it resumes on its own once they pass (no restart needed).</div>';
  // Restart-only drift: the running loop was started on an older coders/repo/…
  // than the config this page (and /status) reads — say so, or the page would
  // report the NEW config as the loop's state.
  if (setup && setup.loop_cfg_stale) html += '<div style="margin-top:6px;font-size:12px"><b>Running loop is stale:</b> ' + esc(String(setup.loop_cfg_stale_hint || "config changed since the loop started")) + '</div>';
  // The D3 advisory (#260): a multi-entry projects: map next to an explicitly blank
  // db_path — inert since D3 (the board runs on the one instance store), so it is
  // an info line, never a failing check.
  if (setup && setup.db_override_ignored) html += '<div style="margin-top:6px;font-size:12px"><b>db_path override ignored:</b> ' + esc(String(setup.db_override_hint || "an explicitly blank db_path resolves to the one instance store")) + '</div>';
  // The D3 migration advisory (#260): a configured repo still carries the pre-D3
  // per-repo `.beads/` workspace the board no longer reads — cards left there are
  // invisible until db_path pins back to that file or they are migrated. Info line,
  // never a failing check.
  if (setup && setup.legacy_store_hint) html += '<div style="margin-top:6px;font-size:12px"><b>Pre-D3 repo workspace detected:</b> ' + esc(String(setup.legacy_store_hint)) + '</div>';
  return html;
}

// Gate-preflight holds (#255/#261): /status carries `held_projects` (sorted names) and
// `preflight.held` ({project: reason}) — the projects whose ready cards the loop froze
// behind a gate that fails on the clean base. Reasons are server-authored operator
// copy, esc()'d — never raw HTML.
function heldGateItems(s){
  const held = (s && s.preflight && s.preflight.held) || {};
  const names = (s && s.held_projects) || [];
  let html = "";
  for (const name of names) html += '<li><b>' + esc(name) + '</b> — ' + esc(String(held[name] || "gate preflight failed")) + '</li>';
  return html;
}

// An otherwise-healthy board with held projects — the holds get their own warning card
// (when setup ALSO fails, renderSetupGaps carries the same list on its card instead).
function renderHeldProjects(s){
  $("err").hidden = false;
  $("err").className = "pl-callout pl-callout--warning";
  // setupLoopLine rides along for the same reason it does on the setup-gap card: this
  // branch OUTRANKS the loop-stale one, so without it a board that is both held and
  // running a stale config shows only the hold — and its "releases on its own, no
  // restart needed" line is then actively wrong, since the restart is what applies the
  // config the operator just changed. Every advisory the suppressed branch would have
  // rendered is carried here instead of being swallowed by priority.
  $("err").innerHTML = '<b>Work is held — a gate fails on its project&#39;s clean base.</b>'
    + '<ul style="margin:6px 0 0 18px;padding:0">' + heldGateItems(s) + '</ul>'
    + '<div style="opacity:.65;margin-top:6px;font-size:12px">The loop re-checks each held project&#39;s gate and releases its cards on its own once the gate passes (no restart needed).</div>'
    + setupLoopLine((s && s.setup) || null);
}

// A bound board whose setup preflight fails — each failing check with its hint, in
// place of a raw error. `e` (optional) is the underlying read error; `s` (optional)
// is the full /status body, so preflight-held projects ride along on this card
// instead of hiding behind the setup gap.
function renderSetupGaps(setup, e, s){
  const blocking = setupBlocking(setup);
  const held = heldGateItems(s);
  $("err").hidden = false;
  $("err").className = "pl-callout pl-callout--warning";
  $("err").innerHTML = '<b>' + (blocking ? 'This board can&#39;t run yet — setup is incomplete.' : 'GitHub CLI missing — PRs can&#39;t open or merge until it is installed.') + '</b>'
    + '<ul style="margin:6px 0 0 18px;padding:0">' + setupGapItems(setup) + '</ul>'
    + setupLoopLine(setup)
    + (held ? '<div style="margin-top:6px"><b>Held projects (gate fails on clean base):</b><ul style="margin:4px 0 0 18px;padding:0">' + held + '</ul></div>' : '')
    + (e ? '<div style="opacity:.65;margin-top:6px;font-size:12px">Underlying error: ' + esc(String(e)) + '</div>' : '');
  $("sub").textContent = blocking ? "project_board — setup incomplete" : "project_board — gh missing";
}

// A healthy board with an advisory only — the running loop on an older config than
// the one this page reads, or an inert db_path override — as an info callout (no
// check is failing, nothing is paused).
function renderLoopStale(setup){
  $("err").hidden = false;
  $("err").className = "pl-callout pl-callout--info";
  $("err").innerHTML = setupLoopLine(setup);
}

// The unbound-board setup card — guidance in place of a red error. Static markup
// + esc()'d error text only; the preflight's OTHER failing checks (br/gh/coder)
// ride along so the operator sees every gap at once, not one per restart.
function renderSetup(e, setup){
  const gaps = setupGapItems(setup ? {...setup, repo: {ok: true}} : null);
  $("err").hidden = false;
  $("err").className = "pl-callout";
  $("err").innerHTML = '<b>This board isn&#39;t bound to a repo yet.</b>'
    + '<ol style="margin:6px 0 0 18px;padding:0">'
    + '<li>Settings &#9656; Plugins &#9656; Project Board &#8594; set <code>repo</code> to the absolute path of the git checkout this agent manages (or <code>db_path</code> to keep the board in a private store).</li>'
    + '<li>Register a coder delegate (Settings &#9656; Delegates) and set <code>project_board.coder</code> to its name — there is no default.</li>'
    + '<li>Turn on <code>loop_enabled</code> when you want the board dispatching builds.</li>'
    + '</ol>'
    + (gaps ? '<div style="margin-top:6px"><b>Also missing:</b><ul style="margin:4px 0 0 18px;padding:0">' + gaps + '</ul></div>' : '')
    + '<div style="opacity:.65;margin-top:6px;font-size:12px">Underlying error: ' + esc(String(e)) + '</div>';
  $("sub").textContent = "project_board — not bound to a repo yet";
}

// ── Kanban: keyed render + motion ─────────────────────────────────────────────────
// The Kanban used to rebuild #kanban via innerHTML on every poll, so nothing could
// animate. It now keeps ONE DOM node per card id (CARD_NODES), reused across renders: a
// render re-writes a card's markup only when it changed, moves the node with
// insertBefore, and animates the move with FLIP — measure every card (First), mutate
// the DOM (Last), apply the inverse transform (Invert), transition it away (Play).
// prefers-reduced-motion gets no movement at all: an instant update plus a brief
// highlight on what changed.
const CARD_NODES = new Map();               // card id → its element, reused across renders
const LANES = {};                           // state → {el, list, count, bar, empty, more}
const RM = window.matchMedia("(prefers-reduced-motion: reduce)");
const MOVE_MS = 440, STAGGER_MS = 55, STAGGER_MAX = 6, ENTER_MS = 360, LEAVE_MS = 260, LAND_MS = 1800;
const EASE = "cubic-bezier(.2,.8,.2,1)";
let FIRST_PAINT = true;
function motionOK(){ return !RM.matches; }
// Classes the motion layer owns — a re-render keeps them while their animation runs.
const TRANSIENT = new Set(["card--enter", "card--flying", "card--landed", "card--flash", "card--placeholder", "card--pending"]);

// Lanes the build loop owns: nothing an operator does here moves a card into or out of
// them — the loop claims on-deck cards, opens the PR, and the merge webhook sets done.
const LOOP_OWNED = new Set(["in_progress", "in_review", "done"]);
const LOOP_WHY = {
  in_progress: "Building — the build loop owns this card until it opens a PR. Read-only here.",
  in_review: "In review — the PR, CI and the review gate move this card. Read-only here.",
  done: "Done — set by the merge. Read-only here.",
};
const LANE_EMPTY = {
  backlog: "Nothing in the backlog.",
  ready: "Nothing on deck. Drag a backlog card here to mark it ready.",
  in_progress: "No builds running.",
  in_review: "Nothing waiting on review.",
  done: "Nothing shipped yet.",
};
// The blocked sweep clears these on its own (loop/_common._SELF_HEALING_BLOCKS); every
// other class waits on a person — the card says which.
const SELF_HEALING = new Set(["rate-limit", "transient", "merge-conflict"]);
const PRIORITY_MIN = 0, PRIORITY_MAX = 4;   // store.validate_priority's range
const LOCK_ICON = '<svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor"'
  + ' stroke-width="1.5" aria-hidden="true"><rect x="3.5" y="7" width="9" height="6.5" rx="1.2"/>'
  + '<path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/></svg>';
const CHECK_ICON = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor"'
  + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7"/></svg>';

// The lane skeleton is built ONCE; renders only patch counts, bars and card lists.
function ensureLanes(){
  const root = $("kanban");
  if (root.dataset.keyed) return;
  root.dataset.keyed = "1";
  root.innerHTML = COLS.map(state => {
    const label = esc(STATE_LABEL[state] || state.replace("_"," "));
    const owned = LOOP_OWNED.has(state);
    return '<section class="lane" data-lane="'+state+'" aria-label="'+label+'">'
      + '<header class="lane-h"><span class="lane-dot" aria-hidden="true"></span>'
      + '<span class="lane-t">'+label+'</span><span class="lane-n" aria-label="cards">0</span>'
      + (owned ? '<span class="lane-lock" title="'+esc(LOOP_WHY[state])+'">'+LOCK_ICON+'</span>' : '')
      + '</header><div class="lane-bar" aria-hidden="true"><i></i></div>'
      + '<div class="lane-why" aria-hidden="true"></div>'
      + '<div class="lane-list" role="list"></div>'
      + '<div class="lane-empty" hidden>'+esc(LANE_EMPTY[state] || "—")+'</div>'
      + '<div class="lane-more"></div></section>';
  }).join("");
  root.querySelectorAll(".lane").forEach(el => {
    LANES[el.dataset.lane] = {el, list: el.querySelector(".lane-list"), count: el.querySelector(".lane-n"),
      bar: el.querySelector(".lane-bar i"), empty: el.querySelector(".lane-empty"), more: el.querySelector(".lane-more"),
      why: el.querySelector(".lane-why")};
  });
}
// The one-line spec summary under a card's title: the first prose line, markdown
// heading/list markers dropped, clipped (CSS clamps it to two lines).
function specLine(s){
  const line = String(s || "").split("\n").map(l => l.replace(/^[\s#>*\-+]+/, "").trim()).find(Boolean) || "";
  return line.length > 160 ? line.slice(0, 157) + "…" : line;
}
// The blocked card's "why" line: the reason, plus whether it clears itself.
function blockedLine(f){
  if (!f.blocked) return "";
  const cls = String(f.blocked_class || "");
  const heal = SELF_HEALING.has(cls)
    ? '<span class="heal heal--auto" title="The blocked sweep clears this on its own">self-healing</span>'
    : (cls === "agent-hold" ? '<span class="heal" title="An agent holds this card until it releases it">agent hold</span>'
      : '<span class="heal heal--you" title="Nothing clears this automatically">needs you</span>');
  const why = String(f.blocked_reason || "");
  return '<div class="why">'+heal+(why ? '<span class="why-t" title="'+esc(why)+'">'+esc(why)+'</span>' : '')+'</div>';
}
function cardHTML(f, state){
  const spec = specLine(f.spec);
  const meta = (flags(f)+' '+taskFoot(f)).trim();
  return '<div class="c-top"><span class="id">'+esc(f.id)+'</span>'
    + '<span class="pri pri-'+esc(String(f.priority))+'" title="priority '+esc(String(f.priority))+' (0 = most urgent)">P'+esc(String(f.priority))+'</span>'
    + (f.project ? '<span class="proj" title="project">'+esc(f.project)+'</span>' : '')
    + '<span class="c-sp"></span>'
    + (state === "done" ? '<span class="c-check" aria-label="done">'+CHECK_ICON+'</span>' : '')
    + '<button class="c-menu" type="button" data-card-menu="'+esc(f.id)+'" aria-haspopup="menu"'
    + ' aria-label="Actions for '+esc(f.id)+'" title="Actions (M)">⋯</button></div>'
    + '<div class="t">'+docIco(f)+esc(f.title)+'</div>'
    + (spec ? '<div class="spec">'+esc(spec)+'</div>' : '')
    + (state === "in_progress" && !isTask(f) && !f.blocked ? '<div class="bld" data-bld></div>' : '')
    + blockedLine(f)
    + (meta ? '<div class="m">'+meta+'</div>' : '');
}
function setData(n, key, v){ if (v == null) n.removeAttribute("data-"+key); else n.setAttribute("data-"+key, v); }
// Patch ONE reused card node to the feature's current state. The markup is rewritten
// only when it changed, so a poll that changed nothing touches nothing.
function updateCard(n, f, state){
  // An in_progress card is live — clicking it opens the coder monitor drawer (#84).
  // A task never dispatches a coder, so it never opens the MONITOR (#217); it opens
  // the same drawer showing its detail + controls (bd-rdmh) via a data-task handle.
  const live = state === "in_progress";
  const mon = live && !isTask(f);
  const owned = LOOP_OWNED.has(state);
  const keep = [...n.classList].filter(c => TRANSIENT.has(c));
  n.className = "card"+(mon?" card--live":"")+(isTask(f)?" card--task":"")
    + (f.blocked ? " card--blocked" : (f.dag_blocked ? " card--waiting" : ""))
    + (owned ? " card--owned" : " card--movable") + (keep.length ? " " + keep.join(" ") : "");
  n.dataset.id = f.id;
  n.dataset.state = state;
  setData(n, "mon", mon ? f.id : null);
  setData(n, "task", isTask(f) ? f.id : null);
  n.tabIndex = 0;
  n.setAttribute("role", "listitem");
  n.setAttribute("aria-keyshortcuts", "M Alt+ArrowRight Alt+ArrowUp Alt+ArrowDown");
  if (owned) n.title = LOOP_WHY[state]; else n.removeAttribute("title");
  const html = cardHTML(f, state);
  if (n._html !== html){ n.innerHTML = html; n._html = html; }
  paintBuilding(n, f);
}
function paintLane(state, total, shown, share, ticks){
  const lane = LANES[state];
  if (lane.count.textContent !== String(total)){
    lane.count.textContent = String(total);
    if (ticks) restartClass(lane.count, "lane-n--tick");
  }
  lane.bar.style.width = share + "%";
  lane.empty.hidden = shown > 0;
  const more = total > shown ? showAllBtn(total) : "";
  if (lane.more._html !== more){ lane.more.innerHTML = more; lane.more._html = more; }
}
function restartClass(el, cls){ el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls); }
function kanbanVisible(){ return !$("kanban").classList.contains("hide"); }

function renderKanban(){
  ensureLanes();
  const animate = motionOK() && !FIRST_PAINT && kanbanVisible() && !document.hidden;
  const first = new Map();
  if (animate) CARD_NODES.forEach((n, id) => { if (n.isConnected) first.set(id, n.getBoundingClientRect()); });
  const focused = document.activeElement && document.activeElement.classList.contains("card") ? document.activeElement.dataset.id : null;
  // Lane contents first, so each lane's bar can show its share of the whole board.
  const lanes = COLS.map(state => {
    let items = FEATURES.filter(f => f.state === state), total = items.length;
    if (state === "done"){ const d = doneSlice(items); items = d.items; total = d.total; }  // cap Done (#115)
    return {state, items, total};
  });
  const all = lanes.reduce((s, l) => s + l.total, 0) || 1;
  const seen = new Set(), entered = [], changed = [], landed = [];
  lanes.forEach(({state, items, total}) => {
    const list = LANES[state].list;
    items.forEach((f, i) => {
      let n = CARD_NODES.get(f.id);
      const isNew = !n;
      if (isNew){ n = document.createElement("div"); CARD_NODES.set(f.id, n); }
      const was = n.dataset.state;
      updateCard(n, f, state);
      if (list.children[i] !== n) list.insertBefore(n, list.children[i] || null);
      seen.add(f.id);
      if (isNew){ if (!FIRST_PAINT) entered.push(n); }
      else if (was !== state){ changed.push(n); if (state === "done") landed.push(n); }
    });
    paintLane(state, total, items.length, Math.round(100 * total / all), !FIRST_PAINT);
  });
  CARD_NODES.forEach((n, id) => { if (!seen.has(id)){ CARD_NODES.delete(id); leave(n, first.get(id), animate); } });
  // insertBefore re-parents a node, which drops its focus — hand it back.
  const fn = focused && CARD_NODES.get(focused);
  if (fn && document.activeElement !== fn) fn.focus({preventScroll: true});
  if (animate) flip(first, entered);
  else if (!FIRST_PAINT) entered.concat(changed).forEach(n => restartClass(n, "card--flash"));
  landed.forEach(land);
  FIRST_PAINT = false;
}
// FLIP: every card that moved since `first` was measured is put back where it was with
// an inverse transform, then transitioned to its new place. A card that changed lane
// flies above the rest; several at once are staggered so the eye can follow each one.
function flip(first, entered){
  const movers = [];
  CARD_NODES.forEach((n, id) => {
    const a = first.get(id);
    if (!a || n.classList.contains("card--placeholder")) return;
    const b = n.getBoundingClientRect();
    const dx = a.left - b.left, dy = a.top - b.top;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
    movers.push({n, dx, dy, far: Math.abs(dx) > 8});
  });
  movers.forEach(m => {
    m.n.style.transition = "none";
    m.n.style.transform = "translate("+m.dx+"px,"+m.dy+"px)";
    if (m.far) m.n.classList.add("card--flying");
  });
  void document.body.offsetWidth;          // commit the inverted frame before playing it
  let k = 0;
  movers.forEach(m => {
    const delay = m.far ? Math.min(k++, STAGGER_MAX) * STAGGER_MS : 0;
    m.n.style.transition = "transform "+MOVE_MS+"ms "+EASE+" "+delay+"ms";
    m.n.style.transform = "";
    clearTimeout(m.n._settle);
    m.n._settle = setTimeout(() => { m.n.style.transition = ""; m.n.classList.remove("card--flying"); }, MOVE_MS + delay + 40);
  });
  entered.forEach((n, i) => {
    n.style.animationDelay = Math.min(i, STAGGER_MAX) * STAGGER_MS + "ms";
    restartClass(n, "card--enter");
    setTimeout(() => { n.classList.remove("card--enter"); n.style.animationDelay = ""; }, ENTER_MS + STAGGER_MAX * STAGGER_MS);
  });
}
// A card that left the board fades out where it was, on the #fx layer, so the lane can
// close the gap underneath it at the same time.
function leave(n, r, animate){
  if (!animate || !r){ n.remove(); return; }
  n.classList.remove("card--flying");
  n.style.cssText = "position:fixed;margin:0;left:"+r.left+"px;top:"+r.top+"px;width:"+r.width+"px;transition:none;transform:none";
  n.classList.add("card--leave");
  $("fx").appendChild(n);
  setTimeout(() => n.remove(), LEAVE_MS + 40);
}
// The moment a card lands in done: a check and a brief glow. Once per arrival.
function land(n){
  restartClass(n, "card--landed");
  clearTimeout(n._land);
  n._land = setTimeout(() => n.classList.remove("card--landed"), LAND_MS);
}

// ── The live "building" strip on in_progress coding cards ──────────────────────────
// Who is coding, for how long, and what it touched last — from the same per-feature
// /progress snapshot the monitor drawer polls, read for the building cards only (an
// in-process ring buffer, no br). The strip's nodes are built once and only their text
// changes, so the elapsed clock can tick every second without restarting the shimmer.
const PROGRESS = new Map();                 // fid → {data, at}
const BUILD_POLL_MS = 4000;
let LAST_BUILD_POLL = 0;
function liveGen(snap){
  const gens = ((snap && snap.gens) || []).slice().sort((a, b) => b.gen - a.gen);
  return gens.find(g => !g.done) || gens[0] || null;
}
function lastLine(s){ const ls = String(s || "").trim().split("\n").filter(Boolean); return ls.length ? ls[ls.length - 1].slice(0, 120) : ""; }
function buildInfo(f){
  const p = PROGRESS.get(f.id), g = liveGen(p && p.data);
  const who = f.assignee || "coder";
  if (!g) return {who: who, gen: "", el: "", act: "starting…"};
  const secs = (Number(g.elapsed_s) || 0) + (g.done ? 0 : (Date.now() - p.at) / 1000);
  const recent = collapseTools(g.recent_tools || []);
  const cur = g.current_tool || recent[recent.length - 1];
  let act = cur ? String(cur.name || "") : (lastLine(g.answer_tail) || "thinking…");
  const loc = cur && (cur.locations || []).find(l => l && act.indexOf(l.split("/").pop()) < 0);
  if (loc) act += " " + loc.split("/").pop();
  return {who: who, gen: g.tier ? g.tier : "gen " + g.gen, el: fmtDur(secs), act: act};
}
function setText(el, t){ if (el && el.textContent !== t) el.textContent = t; }
function paintBuilding(n, f){
  const b = n.querySelector("[data-bld]");
  if (!b) return;
  if (!b.firstChild) b.innerHTML = '<div class="bld-row"><span class="bld-dot" aria-hidden="true"></span>'
    + '<span class="bld-who"></span><span class="bld-gen"></span><span class="bld-el"></span></div>'
    + '<div class="bld-act"></div><div class="bld-bar" aria-hidden="true"><i></i></div>';
  const i = buildInfo(f);
  setText(b.querySelector(".bld-who"), i.who);
  setText(b.querySelector(".bld-gen"), i.gen);
  setText(b.querySelector(".bld-el"), i.el);
  setText(b.querySelector(".bld-act"), i.act);
  b.querySelector(".bld-act").title = i.act;
}
function paintAllBuilding(){
  FEATURES.forEach(f => { if (f.state === "in_progress"){ const n = CARD_NODES.get(f.id); if (n) paintBuilding(n, f); } });
}
async function pollBuilding(){
  LAST_BUILD_POLL = Date.now();
  const ids = FEATURES.filter(f => f.state === "in_progress" && !isTask(f)).slice(0, 8).map(f => f.id);
  for (const fid of ids){
    try { PROGRESS.set(fid, {data: await api(FEAT + encodeURIComponent(fid) + "/progress"), at: Date.now()}); }
    catch (e) { /* keep the last snapshot — the strip says "starting…" until one lands */ }
  }
  for (const k of [...PROGRESS.keys()]) if (!ids.includes(k)) PROGRESS.delete(k);
  paintAllBuilding();
}

// ── Operator moves: the allowed-transition map ────────────────────────────────────
// Only moves that ARE operator actions the API already has. Everything else is
// refused with the reason — the loop owns building / in review / done, and there is
// no un-ready route (a card leaves on deck only when the loop claims it).
//   backlog → on deck   POST /features/{id}/ready   (the Ready gate; 400 names every failure)
//   reorder in backlog / on deck   PATCH /features/{id} {priority}
//   block / unblock (menu)   POST /features/{id}/block {reason} · POST …/unblock
const MOVES = {
  "backlog>ready": "ready",
  "backlog>backlog": "priority",
  "ready>ready": "priority",
};
const NO_UNREADY = "There is no un-ready action: a card leaves on deck only when the loop claims it. Block it to hold it instead.";
// What dropping `f` into lane `to` would do. `lane` is that lane's cards in display
// order WITHOUT `f`; `index` is the slot the drop lands in. → {ok, verb, priority?} or
// {ok: false, why, noop?}.
function planMove(f, to, lane, index){
  const from = f.state;
  if (LOOP_OWNED.has(from)) return {ok: false, why: LOOP_WHY[from]};
  if (LOOP_OWNED.has(to)) return {ok: false, why: "The build loop moves cards into "+(STATE_LABEL[to] || to)+" — it claims on-deck cards itself."};
  const verb = MOVES[from + ">" + to];
  if (!verb) return {ok: false, why: from === "ready" && to === "backlog" ? NO_UNREADY : "Not a move the board supports."};
  const p = dropPriority(f, lane || [], index || 0);
  if (verb === "ready") return p === f.priority ? {ok: true, verb} : {ok: true, verb, priority: p};
  if (p === f.priority) return {ok: false, noop: true, why: "Same priority — cards of one priority are ordered by id."};
  return {ok: true, verb, priority: p};
}
// The priority a drop at `index` implies: moving up takes the rank of the card it lands
// above, moving down (or to the end) the rank of the card it lands below — clamped to
// the store's range. Blocked cards float first regardless of rank, so they don't count.
function dropPriority(f, lane, index){
  const ranked = lane.filter(x => !x.blocked);
  const before = lane.slice(0, index).filter(x => !x.blocked);
  const above = before[before.length - 1], below = ranked[before.length];
  const cur = Number(f.priority);
  let p = cur;
  if (below && (!above || Number(below.priority) < cur)) p = Number(below.priority);
  else if (above && Number(above.priority) > cur) p = Number(above.priority);
  else if (above && below) p = Math.max(Number(above.priority), Math.min(cur, Number(below.priority)));
  return Math.max(PRIORITY_MIN, Math.min(PRIORITY_MAX, p));
}
// The lanes a drag of `f` may drop into (its own lane counts when it can reorder there).
function dropLanes(f){
  return COLS.filter(to => !!MOVES[f.state + ">" + to] && !LOOP_OWNED.has(f.state));
}

// ── Optimistic writes with rollback ───────────────────────────────────────────────
// A move shows at once: the card's new state/priority is patched into FEATURES and
// re-rendered (so it animates), the API call runs, and on an error the board rolls back
// to what it was — the card animates home — with the API's reason in a toast. While the
// call is in flight its patch is re-applied over every poll, so a slow write can't
// flicker the card back and forth.
const PENDING = new Map();                  // fid → patch applied over every load
function sortFeatures(list){
  return list.sort((a,b) => (a.blocked?0:a.state==='in_progress'?1:2) - (b.blocked?0:b.state==='in_progress'?1:2) || a.priority - b.priority || a.id.localeCompare(b.id));
}
function withPending(list){
  if (!PENDING.size) return list;
  return sortFeatures(list.map(f => PENDING.has(f.id) ? {...f, ...PENDING.get(f.id)} : f));
}
async function apiSend(method, p, body){
  const r = await kit.apiFetch(p, {method: method, headers: {"content-type": "application/json"},
    body: JSON.stringify(body || {})});
  const d = await r.json().catch(() => { throw new Error("HTTP " + r.status + " (non-JSON response)"); });
  if (!r.ok) throw new Error(d.detail || "HTTP " + r.status);
  return d;
}
const VERB_LABEL = {ready: "mark ready", priority: "change priority", block: "block", unblock: "unblock"};
const VERB_DONE = {ready: " is on deck", priority: " re-ranked", block: " blocked", unblock: " unblocked"};
async function runMove(f, plan, extra){
  const fid = f.id, prev = FEATURES;
  const patch = plan.verb === "ready" ? {state: "ready", board_state: "ready"}
    : plan.verb === "block" ? {blocked: true, blocked_reason: extra || "", blocked_class: "terminal"}
    : plan.verb === "unblock" ? {blocked: false, blocked_reason: "", blocked_class: ""} : {};
  if (plan.priority != null) patch.priority = plan.priority;
  PENDING.set(fid, patch);
  FEATURES = withPending(FEATURES.slice());
  render();
  const node = CARD_NODES.get(fid);
  if (node) node.classList.add("card--pending");
  try {
    const url = FEAT + encodeURIComponent(fid);
    if (plan.verb === "ready") await apiPost(url + "/ready", {});
    else if (plan.verb === "block") await apiPost(url + "/block", {reason: extra || ""});
    else if (plan.verb === "unblock") await apiPost(url + "/unblock", {});
    if (plan.priority != null) await apiSend("PATCH", url, {priority: plan.priority});
    PENDING.delete(fid);
    toast(fid + (VERB_DONE[plan.verb] || " moved") + (plan.priority != null ? " · P" + plan.priority : ""), "ok");
  } catch (e) {
    PENDING.delete(fid);
    FEATURES = prev;                          // roll back — the card animates home
    render();
    toast("Couldn't " + (VERB_LABEL[plan.verb] || "move") + " " + fid + ": " + ((e && e.message) || e), "error");
  } finally {
    const n = CARD_NODES.get(fid);
    if (n) n.classList.remove("card--pending");
  }
  await load();                               // the server's word is final either way
}

// ── Toasts ────────────────────────────────────────────────────────────────────────
let TOAST_TIMER = null;
function toast(msg, kind){
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast toast--" + (kind || "info") + " toast--on";
  clearTimeout(TOAST_TIMER);
  TOAST_TIMER = setTimeout(() => t.classList.remove("toast--on"), kind === "error" ? 7000 : 2600);
}

// ── Drag and drop — POINTER events, never HTML5 DnD (the desktop app's Tauri webview
// swallows HTML5 drag-and-drop). Press, move past a small threshold, and the card lifts
// into a ghost that follows the pointer; the card's own node stays behind as a
// placeholder that slides (FLIP) into the slot the drop would take. Lanes the card can't
// go to dim and say why; a refused or cancelled drop snaps the ghost back home.
const DRAG_THRESHOLD = 6;
let PRESS = null, DRAG = null, SUPPRESS_CLICK = false, RENDER_DEFERRED = false;
function featureById(id){ return FEATURES.find(f => f.id === id); }
function laneAt(x, y){
  for (const state of COLS){
    const r = LANES[state].el.getBoundingClientRect();
    if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return state;
  }
  return null;
}
function slotIndex(list, y, skip){
  const cards = [...list.children].filter(n => n !== skip);
  for (let i = 0; i < cards.length; i++){
    const r = cards[i].getBoundingClientRect();
    if (y < r.top + r.height / 2) return i;
  }
  return cards.length;
}
// FLIP for the cards in a few lanes around one DOM change (the placeholder sliding).
function flipAround(mutate){
  if (!motionOK()){ mutate(); return; }
  const first = new Map();
  CARD_NODES.forEach((n, id) => first.set(id, n.getBoundingClientRect()));
  mutate();
  CARD_NODES.forEach((n, id) => {
    const a = first.get(id), b = n.getBoundingClientRect();
    if (n.classList.contains("card--placeholder")) return;
    const dx = a.left - b.left, dy = a.top - b.top;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
    n.style.transition = "none"; n.style.transform = "translate("+dx+"px,"+dy+"px)";
    void n.offsetWidth;
    n.style.transition = "transform 200ms " + EASE; n.style.transform = "";
  });
}
function onPointerDown(e){
  if (e.button !== 0 || DRAG || VIEW !== "kanban") return;
  const n = e.target.closest(".card");
  if (!n || !$("kanban").contains(n) || e.target.closest("a,button,input,textarea")) return;
  PRESS = {n, id: n.dataset.id, x: e.clientX, y: e.clientY, pid: e.pointerId};
}
function onPointerMove(e){
  if (DRAG){ dragMove(e); return; }
  if (!PRESS || e.pointerId !== PRESS.pid) return;
  if (Math.hypot(e.clientX - PRESS.x, e.clientY - PRESS.y) < DRAG_THRESHOLD) return;
  const f = featureById(PRESS.id), n = PRESS.n;
  PRESS = null;
  // A press that travels is a drag gesture, never a text selection.
  document.body.classList.add("is-pressing");
  const sel = window.getSelection && window.getSelection();
  if (sel) sel.removeAllRanges();
  SUPPRESS_CLICK = true;                      // a drag is never also a click
  if (!f) return;
  if (LOOP_OWNED.has(f.state)){ toast(LOOP_WHY[f.state], "info"); restartClass(n, "card--refuse"); return; }
  startDrag(e, f, n);
}
function startDrag(e, f, n){
  const r = n.getBoundingClientRect();
  const ghost = n.cloneNode(true);
  ghost.classList.remove("card--enter", "card--landed", "card--flash", "card--flying");
  ghost.classList.add("card--ghost");
  ghost.removeAttribute("data-mon"); ghost.removeAttribute("data-task"); ghost.removeAttribute("tabindex");
  ghost.style.cssText = "position:fixed;margin:0;left:"+r.left+"px;top:"+r.top+"px;width:"+r.width+"px";
  $("fx").appendChild(ghost);
  const home = {lane: f.state, index: [...LANES[f.state].list.children].indexOf(n)};
  DRAG = {f, n, ghost, dx: e.clientX - r.left, dy: e.clientY - r.top, home, over: f.state, ok: dropLanes(f)};
  n.classList.add("card--placeholder");
  document.body.classList.add("is-dragging");
  COLS.forEach(state => {
    const el = LANES[state].el, ok = DRAG.ok.includes(state);
    el.classList.toggle("lane--drop-ok", ok);
    el.classList.toggle("lane--drop-no", !ok);
    LANES[state].why.textContent = ok ? "" : planMove(f, state, [], 0).why;
  });
  try { n.setPointerCapture(e.pointerId); } catch (_e) { /* the card may have re-rendered */ }
  dragMove(e);
}
function dragMove(e){
  const d = DRAG;
  d.ghost.style.transform = "translate("+(e.clientX - d.dx - parseFloat(d.ghost.style.left))+"px,"
    +(e.clientY - d.dy - parseFloat(d.ghost.style.top))+"px) rotate(1.5deg)";
  const over = laneAt(e.clientX, e.clientY);
  COLS.forEach(state => LANES[state].el.classList.toggle("lane--over", state === over));
  d.over = over;
  const target = over && d.ok.includes(over) ? over : d.home.lane;
  const list = LANES[target].list;
  const idx = target === over ? slotIndex(list, e.clientY, d.n) : d.home.index;
  const cards = [...list.children].filter(x => x !== d.n);
  const ref = cards[idx] || null;
  if (d.n.parentNode !== list || d.n.nextSibling !== ref) flipAround(() => list.insertBefore(d.n, ref));
}
function endDrag(drop){
  const d = DRAG;
  if (!d) return;
  DRAG = null;
  document.body.classList.remove("is-dragging");
  COLS.forEach(state => LANES[state].el.classList.remove("lane--drop-ok", "lane--drop-no", "lane--over"));
  const to = d.n.parentNode && d.n.parentNode.parentNode && d.n.parentNode.parentNode.dataset.lane;
  const lane = [...LANES[to].list.children].filter(x => x !== d.n).map(x => featureById(x.dataset.id)).filter(Boolean);
  const index = [...LANES[to].list.children].indexOf(d.n);
  const plan = drop && d.over === to ? planMove(d.f, to, lane, index) : {ok: false, noop: true};
  if (drop && d.over && d.over !== to) toast(planMove(d.f, d.over, [], 0).why, "info");
  else if (drop && !plan.ok && !plan.noop) toast(plan.why, "info");
  if (plan.ok){
    runMove(d.f, plan);                       // optimistic: re-renders at once (FLIP)
  } else {
    // Snap back: the placeholder returns to its home slot, then the ghost flies to it.
    const home = LANES[d.home.lane].list, sib = [...home.children].filter(x => x !== d.n)[d.home.index] || null;
    flipAround(() => home.insertBefore(d.n, sib));
  }
  if (RENDER_DEFERRED){ RENDER_DEFERRED = false; render(); }
  settleGhost(d);
}
// The ghost glides onto wherever the card's node ended up, then hands back to it.
function settleGhost(d){
  const r = d.n.getBoundingClientRect();
  const g = d.ghost;
  const tx = r.left - parseFloat(g.style.left), ty = r.top - parseFloat(g.style.top);
  const done = () => { g.remove(); d.n.classList.remove("card--placeholder"); restartClass(d.n, "card--flash"); };
  if (!motionOK()){ done(); return; }
  g.style.transition = "transform 260ms " + EASE + ", box-shadow 260ms " + EASE;
  g.style.transform = "translate("+tx+"px,"+ty+"px)";
  g.classList.add("card--ghost-drop");
  setTimeout(done, 280);
}
function onPointerUp(e){
  document.body.classList.remove("is-pressing");
  if (PRESS && e.pointerId === PRESS.pid) PRESS = null;
  if (DRAG) endDrag(e.type === "pointerup");
}
document.addEventListener("pointerdown", onPointerDown);
document.addEventListener("pointermove", onPointerMove);
document.addEventListener("pointerup", onPointerUp);
document.addEventListener("pointercancel", onPointerUp);
// A drag (or a refused drag) ends in a click on the card — swallow exactly that one.
document.addEventListener("click", (e) => {
  if (!SUPPRESS_CLICK) return;
  SUPPRESS_CLICK = false;
  if (e.target.closest(".card")) { e.stopPropagation(); e.preventDefault(); }
}, true);
document.addEventListener("pointerdown", () => { SUPPRESS_CLICK = false; }, true);

// ── The keyboard path: every drag has a menu/shortcut twin ─────────────────────────
// Focus a card (Tab), then M (or the ⋯ button, or the context-menu key) opens its
// action menu; Alt+→ marks it ready, Alt+↑/↓ raise/lower its priority. Arrow keys walk
// the menu, Enter picks, Esc closes and hands focus back to the card.
function menuItems(f){
  const items = [];
  if (LOOP_OWNED.has(f.state)) items.push({id: "owned", label: LOOP_WHY[f.state], disabled: true});
  if (f.state === "backlog") items.push({id: "ready", label: "Mark ready → on deck", key: "Alt+→"});
  if (f.state === "backlog" || f.state === "ready"){
    items.push({id: "up", label: "Raise priority (P"+f.priority+" → P"+Math.max(PRIORITY_MIN, f.priority - 1)+")", key: "Alt+↑", disabled: f.priority <= PRIORITY_MIN});
    items.push({id: "down", label: "Lower priority (P"+f.priority+" → P"+Math.min(PRIORITY_MAX, f.priority + 1)+")", key: "Alt+↓", disabled: f.priority >= PRIORITY_MAX});
  }
  if (f.state === "ready") items.push({id: "unready", label: "Back to backlog — no un-ready action", disabled: true, why: NO_UNREADY});
  if (f.state !== "done" && f.state !== "cancelled") items.push(f.blocked ? {id: "unblock", label: "Unblock"} : {id: "block", label: "Block…"});
  if (f.state === "in_progress" && !isTask(f)) items.push({id: "open", label: "Open coder monitor"});
  else if (isTask(f)) items.push({id: "open", label: "Open task"});
  return items;
}
function planFor(f, id){
  if (id === "ready") return {ok: true, verb: "ready"};
  if (id === "up" || id === "down"){
    const p = Math.max(PRIORITY_MIN, Math.min(PRIORITY_MAX, f.priority + (id === "up" ? -1 : 1)));
    return p === f.priority ? {ok: false, noop: true} : {ok: true, verb: "priority", priority: p};
  }
  if (id === "block" || id === "unblock") return {ok: true, verb: id};
  return {ok: false, noop: true};
}
let MENU_FOR = null;
function openMenu(fid, anchor){
  const f = featureById(fid);
  if (!f) return;
  MENU_FOR = fid;
  const m = $("move-menu");
  m.innerHTML = '<div class="mm-h">'+esc(fid)+'</div>' + menuItems(f).map(it =>
    '<button type="button" role="menuitem" class="mm-i" data-mm="'+it.id+'"'+(it.disabled ? ' aria-disabled="true"' : '')
    + (it.why ? ' title="'+esc(it.why)+'"' : '')+'><span>'+esc(it.label)+'</span>'
    + (it.key ? '<kbd>'+esc(it.key)+'</kbd>' : '')+'</button>').join("");
  m.hidden = false;
  const r = anchor.getBoundingClientRect(), mw = m.offsetWidth, mh = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(window.innerWidth - mw - 8, r.right - mw)) + "px";
  m.style.top = (r.bottom + mh + 8 > window.innerHeight ? Math.max(8, r.top - mh - 4) : r.bottom + 4) + "px";
  const firstOn = m.querySelector('.mm-i:not([aria-disabled])') || m.querySelector(".mm-i");
  if (firstOn) firstOn.focus();
}
function closeMenu(refocus){
  const m = $("move-menu");
  if (m.hidden) return false;
  m.hidden = true;
  const n = MENU_FOR && CARD_NODES.get(MENU_FOR);
  MENU_FOR = null;
  if (refocus && n) n.focus({preventScroll: true});
  return true;
}
function blockForm(fid){
  const m = $("move-menu");
  m.innerHTML = '<div class="mm-h">Block '+esc(fid)+'</div><form class="mm-form" data-mm-block="'+esc(fid)+'">'
    + '<input class="tin" name="reason" required placeholder="Why? (stays until someone unblocks it)" aria-label="Block reason">'
    + '<div class="trow"><button class="tbtn tbtn--danger" type="submit">Block</button>'
    + '<button class="tbtn" type="button" data-mm="cancel">Cancel</button></div></form>';
  m.querySelector("input").focus();
}
function pickMenu(id){
  const f = featureById(MENU_FOR);
  if (!f) { closeMenu(false); return; }
  if (id === "block"){ blockForm(f.id); return; }
  if (id === "cancel"){ closeMenu(true); return; }
  if (id === "open"){ closeMenu(false); if (isTask(f)) openTask(f.id); else openMonitor(f.id); return; }
  closeMenu(true);
  const plan = planFor(f, id);
  if (plan.ok) runMove(f, plan);
}
$("move-menu").addEventListener("click", (e) => {
  const b = e.target.closest("[data-mm]");
  if (!b || b.getAttribute("aria-disabled") === "true") return;
  pickMenu(b.getAttribute("data-mm"));
});
$("move-menu").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target.closest("[data-mm-block]");
  const reason = form.querySelector("input").value.trim();
  if (!reason) return;
  const f = featureById(form.getAttribute("data-mm-block"));
  closeMenu(true);
  if (f) runMove(f, {ok: true, verb: "block"}, reason);
});
$("move-menu").addEventListener("keydown", (e) => {
  const items = [...$("move-menu").querySelectorAll(".mm-i")];
  const i = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown" || e.key === "ArrowUp"){
    e.preventDefault();
    if (items.length) items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
  } else if (e.key === "Tab"){ closeMenu(true); }
});
document.addEventListener("pointerdown", (e) => { if (!e.target.closest("#move-menu,[data-card-menu]")) closeMenu(false); });
document.addEventListener("keydown", (e) => {
  const n = e.target.closest && e.target.closest(".card");
  if (!n || !$("kanban").contains(n) || e.target !== n) return;
  const f = featureById(n.dataset.id);
  if (!f) return;
  if (e.key === "m" || e.key === "M" || e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")){
    e.preventDefault(); openMenu(f.id, n.querySelector("[data-card-menu]") || n); return;
  }
  if (e.key === "Enter" || e.key === " "){
    e.preventDefault();
    if (n.hasAttribute("data-mon") || n.hasAttribute("data-task")) n.click();
    else openMenu(f.id, n.querySelector("[data-card-menu]") || n);
    return;
  }
  const shortcut = e.altKey && {ArrowRight: "ready", ArrowUp: "up", ArrowDown: "down"}[e.key];
  if (shortcut){
    e.preventDefault();
    if (LOOP_OWNED.has(f.state)) { toast(LOOP_WHY[f.state], "info"); return; }
    if (shortcut === "ready" && f.state !== "backlog") { toast(f.state === "ready" ? "Already on deck." : NO_UNREADY, "info"); return; }
    const plan = planFor(f, shortcut);
    if (plan.ok) runMove(f, plan);
  }
});

// ── Live coder monitor drawer (#84) ────────────────────────────────────────────
// Clicking an in_progress card/row opens a right-edge slide-over that polls the
// per-feature progress snapshot every ~3s, mirroring the console's goal-detail-
// drawer UX in this page's OWN vanilla HTML/JS (an iframe — no console imports).
const MON_POLL_MS = 3000;
// MON_FID: the coding feature the monitor is polling (null when closed OR when the
// shared drawer is showing a task instead). TASK_FID: the task whose detail the shared
// drawer is showing (null otherwise). The two are mutually exclusive — opening one
// clears the other — and both fence writes to the shared #drawer-body (see pollMonitor
// / syncTaskDrawer) so a stale async can't clobber a re-purposed drawer.
let MON_FID = null, MON_TIMER = null, TASK_FID = null, MON_LAST_FID = null;
// The single-card detail behind the OPEN task drawer (#312): the list projection the 10s
// poll pulls (/features) never carries a task's full deliverable — only a small signal
// (`delivered`, `deliverable_chars`, a short `deliverable_preview`, #399) — so the drawer
// would render no deliverable for a delivered task. So the drawer fetches the single-
// feature route (/features/{fid} → get_feature → br show, which carries comments) ON
// OPEN and after each action, caching the result here so
// the 10s poll's syncTaskDrawer re-render reuses it (no per-task br show every tick, the
// monitor's on-open /progress posture). Shape: {fid, feature} on success, {fid, error} on
// a failed fetch (surfaced in the drawer); null when no task is open / not yet fetched.
let TASK_DETAIL = null;
// Monotonic ticket for fetchTaskDetail — see its comment: fences two in-flight
// fetches of the SAME task so a slow earlier one cannot overwrite a newer one.
const TASK_DETAIL_SEQ = {};

// One tool row: status, the tool's own title, and its file(s) only when the title does
// not already name them. ACP's kind (read/edit/execute) rides a class for the colour,
// never as a raw "[edit]" tag. One line, ellipsized; the full text is the tooltip.
const TOOL_STATUS_WORD = {start: "running", running: "running", completed: "done", failed: "failed"};
function toolLine(t){
  const raw = t.status || "";
  const word = esc(TOOL_STATUS_WORD[raw] || raw);
  const name = String(t.name || "tool");
  const locs = (t.locations || []).filter(l => l && name.indexOf(l) < 0 && name.indexOf(l.split("/").pop()) < 0);
  const loc = locs.length ? ' <span class="loc">'+esc(locs.join(", "))+'</span>' : "";
  const rep = t.count > 1 ? '<span class="rep">×'+t.count+'</span>' : "";
  const full = word + " " + name + (locs.length ? " " + locs.join(", ") : "") + (t.count > 1 ? " (×"+t.count+")" : "");
  return '<li class="k-'+esc(t.kind||"")+'" title="'+esc(full)+'"><span class="st st-'+word+'"><span class="sr">'+word+'</span></span>'
    + '<span class="tn">'+esc(name)+'</span>'+loc+rep+'</li>';
}
// Consecutive identical calls (four Edits of the same file, a run of bare `shell`s) read as
// ONE row with a ×N count instead of a wall of repeats.
function groupTools(rows){
  const out = [];
  for (const r of rows){
    const prev = out[out.length - 1];
    if (prev && prev.name === r.name && prev.status === r.status && String(prev.locations) === String(r.locations)){
      prev.count = (prev.count || 1) + 1; continue;
    }
    out.push(Object.assign({}, r, {count: 1}));
  }
  return out;
}
// The feed lists every call ONCE: a call's end replaces its own start row in place (by
// tool id, else by name for an older buffer without ids), so the drawer shows "done Edit
// cli.py", not a "start" and a "completed" line for the same edit.
function collapseTools(rows){
  const out = [], open = new Map();
  for (const r of rows){
    const key = r.id || r.name || "";
    if (r.status === "start"){ open.set(key, out.length); out.push(r); continue; }
    if (open.has(key)){ out[open.get(key)] = r; open.delete(key); continue; }
    out.push(r);
  }
  return out;
}
// ── "saying" markdown (bd-p87t): the coder's streamed answer_tail is markdown prose
// (headings, code blocks, inline code, bold/italic, lists, links), so the "saying"
// section renders it THROUGH a client-side markdown pass instead of showing raw esc()'d
// syntax. The renderer (marked) loads LAZILY from CDN on the first drawer open and is
// cached; if it never loads, the section keeps the plain esc()'d text genCard emits
// inline (the fallback). "thinking" (thought_tail) is internal reasoning, not prose, so
// it stays plain esc()'d text — only "saying" is upgraded.
const MARKED_CDN = "https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js";
// Subresource-integrity pin for the 12.0.2 artifact (cdnjs-published sha512): the script
// only executes if the fetched bytes match, so a tampered CDN response fails closed into
// s.onerror → the plain esc()'d fallback. crossorigin=anonymous is what makes the
// integrity check enforceable on a cross-origin script.
const MARKED_SRI = "sha512-xeUh+KxNyTufZOje++oQHstlMQ8/rpyzPuM+gjMFYK3z5ILJGE7l2NvYL+XfliKURMpBIKKp1XoPN/qswlSMFA==";
let MARKED = null, MARKED_LOAD = null;
function loadMarked(){
  if (MARKED_LOAD) return MARKED_LOAD;                    // load once; cache the promise
  MARKED_LOAD = new Promise((resolve) => {
    if (window.marked) return resolve(window.marked);
    const s = document.createElement("script");
    s.src = MARKED_CDN;
    s.integrity = MARKED_SRI;
    s.crossOrigin = "anonymous";
    s.async = true;                                       // lazy — never blocks initial paint
    s.onload = () => resolve(window.marked || null);
    s.onerror = () => resolve(null);                      // CDN blocked/offline → plain-text fallback
    (document.head || document.documentElement).appendChild(s);
  }).then((m) => (MARKED = m || null));
  return MARKED_LOAD;
}
// Parse markdown → HTML with a hard XSS guard: escape angle brackets in the SOURCE first,
// so any HTML tags in the coder's text render as literal characters, never live markup
// (marked passes source HTML through by default). Markdown syntax (#, *, `, -, [](), etc.)
// is angle-bracket-free, so it is untouched and still renders. breaks:true keeps the
// coder's newlines; gfm:true enables fenced code + tables.
function renderMarkdown(marked, src){
  const safe = String(src).replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const parse = (marked && marked.parse) ? marked.parse : marked;   // marked.parse spans UMD versions
  return parse(safe, {breaks: true, gfm: true});
}
// Belt-and-suspenders on top of the source escape: neutralize the only attributes markdown
// syntax can emit (link href / image src) by dropping dangerous URL schemes, and open any
// links safely in a new tab.
function sanitizeSaying(el){
  el.querySelectorAll("a[href],img[src]").forEach((n) => {
    const attr = n.tagName === "A" ? "href" : "src";
    if (/^\s*(?:javascript|data|vbscript):/i.test(n.getAttribute(attr) || "")) n.removeAttribute(attr);
    if (n.tagName === "A") { n.setAttribute("target", "_blank"); n.setAttribute("rel", "noopener noreferrer nofollow"); }
  });
}
// Upgrade every "saying" block in the just-rendered drawer from its plain esc()'d fallback
// to rendered markdown. Lazy + best-effort: synchronous (no flash) once the lib is cached,
// a no-op if the CDN load fails (the esc()'d fallback stays). Re-runs on each poll
// re-render, since renderMonitor rebuilds innerHTML into fresh nodes.
function enhanceSaying(root){
  const nodes = root.querySelectorAll(".md-saying");
  if (!nodes.length) return;
  const apply = (marked) => {
    if (!marked) return;                                  // load failed → keep the esc()'d fallback
    nodes.forEach((el) => {
      if (el.dataset.mdOn) return;
      el.innerHTML = renderMarkdown(marked, el.getAttribute("data-md") || "");
      sanitizeSaying(el);
      el.classList.add("md-on");                          // swaps pre-wrap → block flow + markdown CSS
      el.dataset.mdOn = "1";
    });
  };
  if (MARKED) apply(MARKED);                              // already cached → upgrade synchronously
  else loadMarked().then(apply);
}
// ── Gen card layout (monitor v2): every section takes its NATURAL height and the drawer
// body is the ONE scroll. The old lone-gen layout forced the card to 100% of the drawer as a
// flex column, so plan / saying / tools each shrank into its own tiny scrollbox and clipped
// mid-line. Long sections now clamp to a short tail (newest content visible, older content
// fading out at the top, no inner scrollbar) behind a "show all" toggle, and the expansion
// state survives the 3s re-render (MON_UI) — a poll never snaps an open section shut.
const MON_UI = {};                       // "<gen>:<section>" → true (expanded) / false (collapsed)
const ACTIVITY_ROWS = 8;
function fmtDur(s){
  s = Math.max(0, Math.round(Number(s) || 0));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
  return Math.floor(s / 3600) + "h " + String(Math.floor(s % 3600 / 60)).padStart(2, "0") + "m";
}
function fmtTok(n){
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(n % 1e6 ? 1 : 0).replace(/\.0$/, "") + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1).replace(/\.0$/, "") + "k";
  return String(n);
}
function uiOpen(key, dflt){ return key in MON_UI ? MON_UI[key] : dflt; }
// One section: a header button (label, a short meta, a chevron) over its body. `collapsible`
// sections hide their body when closed; `clampKey` adds the tail clamp + show-all toggle.
function section(gen, key, label, meta, body, opts){
  opts = opts || {};
  const k = gen + ":" + key;
  const open = opts.collapsible ? uiOpen(k, opts.open !== false) : true;
  let h = '<section class="sec sec-'+key+(open ? "" : " closed")+'">'
    + '<button class="sec-h" type="button"'+(opts.collapsible ? ' data-mon-toggle="'+esc(k)+'" data-open="'+open+'" aria-expanded="'+open+'"' : ' tabindex="-1"')+'>'
    + '<span class="sec-l">'+esc(label)+'</span>'
    + (meta ? '<span class="sec-m">'+meta+'</span>' : "")
    + (opts.collapsible ? '<span class="chev" aria-hidden="true"></span>' : "")
    + '</button>';
  if (open){
    if (opts.clampKey){
      const full = uiOpen(gen + ":" + opts.clampKey, false);
      h += '<div class="sec-b"><div class="tail'+(full ? " full" : "")+'">'+body+'</div>'
        + '<button class="more" type="button" data-mon-toggle="'+esc(gen + ":" + opts.clampKey)+'" data-open="'+full+'">'+(full ? "Show less" : "Show all")+'</button></div>';
    } else h += '<div class="sec-b">'+body+'</div>';
  }
  return h + '</section>';
}
function genCard(g){
  const gen = String(g.gen);
  const live = !g.done;
  const state = live ? '<span class="gs live"><span class="dot"></span>running</span>'
    : '<span class="gs '+(g.stop_reason && g.stop_reason !== "end_turn" ? "stopped" : "ended")+'">'
      + esc(g.stop_reason && g.stop_reason !== "end_turn" ? "stopped · " + g.stop_reason : "finished")+'</span>';
  // A finished gen folds to its summary (header, meter, verify) so the live one stays in
  // view; its header expands it.
  const body = uiOpen(gen + ":body", live);
  let h = '<div class="gen'+(live ? " gen-live" : "")+(body ? "" : " folded")+'"><div class="gh"'
    + (live ? "" : ' data-mon-toggle="'+esc(gen + ":body")+'" data-open="'+body+'" role="button" tabindex="0" aria-expanded="'+body+'"')+'>'
    + '<span class="gn">Gen '+esc(gen)+'</span>'
    + (g.tier ? '<span class="tier">'+esc(g.tier)+'</span>' : "")
    + '<span class="gh-r">'+state+'<span class="el">'+esc(fmtDur(g.elapsed_s))+'</span>'
    + (live ? "" : '<span class="chev" aria-hidden="true"></span>')+'</span></div>';
  if (g.usage && g.usage.size){
    const pct = Math.min(100, Math.round(100 * (Number(g.usage.used) || 0) / Number(g.usage.size)));
    h += '<div class="meter" title="'+esc(String(g.usage.used))+' / '+esc(String(g.usage.size))+' tokens">'
      + '<div class="bar"><div class="fill'+(pct >= 80 ? " hot" : "")+'" style="width:'+pct+'%"></div></div>'
      + '<span class="mt">'+esc(fmtTok(g.usage.used))+' / '+esc(fmtTok(g.usage.size))+' tokens · '+pct+'%</span></div>';
  }
  if (g.verify){
    h += section(gen, "verify", "Verify", "", '<div class="vrow"><span class="st st-'+(g.verify.passed ? "done" : "failed")+'"><span class="sr">'
      + (g.verify.passed ? "passed" : "failed")+'</span></span><span class="vt">'+(g.verify.passed ? "passed" : "failed")+'</span> <code>'
      + esc(g.verify.test_cmd||"")+'</code></div>');
  }
  if (!body) return h + '</div>';
  // Now: the current call — its title, its file(s) unless the title names them, and ONE plain
  // line of what it does (server-side `detail`), clamped to two lines; the row expands in place.
  const cur = g.current_tool;
  if (cur){
    const cname = String(cur.name || "");
    const clocs = (cur.locations || []).filter(l => l && cname.indexOf(l) < 0 && cname.indexOf(l.split("/").pop()) < 0);
    const detail = cur.detail !== undefined ? (cur.detail && cur.detail !== cname ? cur.detail : "") : (cur.input_preview || "");
    const word = TOOL_STATUS_WORD[cur.status] || cur.status || "";
    const wide = uiOpen(gen + ":cur-full", false);
    const row = '<div class="cur'+(wide ? " full" : "")+'" data-mon-toggle="'+esc(gen + ":cur-full")+'" data-open="'+wide+'" title="'+esc(wide ? "Click to collapse" : "Click to expand")+'">'
      + '<span class="st st-'+esc(word)+'"><span class="sr">'+esc(word)+'</span></span>'
      + '<div class="cur-t"><div class="cn">'+esc(cname)+(clocs.length ? ' <span class="loc">'+esc(clocs.join(", "))+'</span>' : "")+'</div>'
      + (detail ? '<div class="inprev">'+esc(detail)+'</div>' : "")+'</div></div>';
    h += section(gen, "now", live && word === "running" ? "Now" : "Last tool", "", row);
  }
  // Plan: the coder's own live todo list. Finished steps fold into one "N done" row once the
  // list is long, so the in-progress step is always on screen.
  if (g.plan && g.plan.length){
    const done = g.plan.filter(e => e.status === "completed").length;
    const showDone = uiOpen(gen + ":plan-done", g.plan.length <= 6);
    const li = (e) => {
      const st = e.status || "";
      const glyph = st === "completed" ? "✓" : (st === "in_progress" ? "▸" : "○");
      return '<li class="pl-'+esc(st)+'"><span class="glyph">'+glyph+'</span><span>'+esc(e.content||"")+'</span></li>';
    };
    let items = g.plan;
    let fold = "";
    if (!showDone && done){
      items = g.plan.filter(e => e.status !== "completed");
      fold = '<li class="fold"><button class="more" type="button" data-mon-toggle="'+esc(gen + ":plan-done")+'" data-open="false">✓ '+done+' done — show</button></li>';
    }
    h += section(gen, "plan", "Plan", done + " / " + g.plan.length, '<ul class="plan">'+fold+items.map(li).join("")+'</ul>', {collapsible: true});
  }
  // "saying" is markdown — the raw source rides data-md and esc()'d text renders inline as the
  // fallback; enhanceSaying() upgrades it once marked loads. The tail clamp keeps the NEWEST
  // narration in view with no inner scrollbar.
  if (g.answer_tail){
    h += section(gen, "saying", "Saying", "", '<div class="thought md-saying" data-md="'+esc(g.answer_tail)+'">'+esc(g.answer_tail)+'</div>',
      {collapsible: true, clampKey: "saying-full"});
  }
  // "thinking" stays plain esc()'d text (internal reasoning, not prose) and starts collapsed.
  if (g.thought_tail){
    h += section(gen, "thinking", "Thinking", "", '<div class="thought">'+esc(g.thought_tail)+'</div>',
      {collapsible: true, open: false, clampKey: "thinking-full"});
  }
  const calls = collapseTools(g.recent_tools||[]);
  if (calls.length){
    const rows = groupTools(calls.slice().reverse());
    const all = uiOpen(gen + ":tools-all", false);
    const shown = all ? rows : rows.slice(0, ACTIVITY_ROWS);
    const more = rows.length > ACTIVITY_ROWS
      ? '<button class="more" type="button" data-mon-toggle="'+esc(gen + ":tools-all")+'" data-open="'+all+'">'+(all ? "Show less" : "Show all "+rows.length)+'</button>' : "";
    h += section(gen, "tools", "Activity", calls.length + (calls.length === 1 ? " call" : " calls"),
      '<ul class="tools">'+shown.map(toolLine).join("")+'</ul>'+more, {collapsible: true});
  }
  return h + '</div>';
}
let MON_LAST = "";
function renderMonitor(data, force){
  const gens = (data && data.gens) || [];
  // An unchanged snapshot is not re-rendered: rebuilding innerHTML every 3s resets text
  // selection and re-runs the markdown pass for nothing.
  const sig = JSON.stringify(gens);
  if (!force && sig === MON_LAST) return;
  MON_LAST = sig;
  const body = $("drawer-body");
  const top = body.scrollTop;
  body.innerHTML = gens.length
    ? gens.slice().sort((a, b) => (a.done ? 1 : 0) - (b.done ? 1 : 0) || b.gen - a.gen).map(genCard).join("")
    : '<div class="pl-empty">No live coder run for this feature right now.</div>';
  body.scrollTop = top;                 // a poll never yanks the reader back to the top
  enhanceSaying($("drawer-body"));   // upgrade "saying" plain text → rendered markdown (lazy, best-effort)
}
let MON_DATA = null;
// Section toggles (headers, "show all", the current-tool row) flip MON_UI and re-render from
// the last snapshot immediately — no wait for the next poll.
$("drawer-body").addEventListener("click", (e) => {
  const t = e.target.closest("[data-mon-toggle]");
  if (!t || !MON_FID) return;
  MON_UI[t.getAttribute("data-mon-toggle")] = t.getAttribute("data-open") !== "true";
  if (MON_DATA) renderMonitor(MON_DATA, true);
});
$("drawer-body").addEventListener("keydown", (e) => {
  const t = e.target.closest('[data-mon-toggle][role="button"]');
  if (t && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); t.click(); }
});
async function pollMonitor(){
  // Fence on the fid we START the request for: clearInterval stops FUTURE polls but
  // cannot cancel a request already in flight, so if the drawer is switched to a task
  // (or closed) while this awaits, MON_FID no longer matches and we bail WITHOUT
  // writing — a late success or error must not clobber a re-purposed #drawer-body.
  const fid = MON_FID;
  if (!fid) return;
  try {
    const data = await api("/api/plugins/project_board/features/"+encodeURIComponent(fid)+"/progress");
    if (MON_FID !== fid) return;
    MON_DATA = data;
    renderMonitor(data);
  } catch (e) {
    if (MON_FID !== fid) return;
    $("drawer-body").innerHTML = '<div class="pl-callout pl-callout--error">'+esc(""+e)+'</div>';
  }
}
function openMonitor(fid){
  TASK_FID = null;                                               // switching drawer modes → not a task
  MON_FID = fid;
  $("drawer-title").textContent = "Coder monitor — " + fid;
  if (fid !== MON_LAST_FID){ for (const k in MON_UI) delete MON_UI[k]; MON_LAST_FID = fid; }
  MON_LAST = ""; MON_DATA = null;
  $("drawer").classList.add("open"); $("scrim").classList.add("open");
  document.body.classList.add("drawer-open");                    // lock page scroll behind the scrim
  $("drawer-body").innerHTML = '<div class="pl-empty">Loading…</div>';
  pollMonitor();
  if (MON_TIMER) clearInterval(MON_TIMER);
  MON_TIMER = setInterval(pollMonitor, MON_POLL_MS);
}
// Open a task's detail in the SAME drawer (bd-rdmh): a task has no coder run, so there
// is NO polling — clear any monitor state first (MON_FID=null fences a poll already in
// flight from clobbering this body; see pollMonitor), then render the static detail.
function openTask(fid){
  MON_FID = null;
  if (MON_TIMER) { clearInterval(MON_TIMER); MON_TIMER = null; }
  TASK_FID = fid;
  TASK_DETAIL = null;                                            // drop the prior task's fetched detail
  VERIFY_NOTE = null;                                            // …and any approval note it carried
  $("drawer-title").textContent = "Task — " + fid;
  $("drawer").classList.add("open"); $("scrim").classList.add("open");
  document.body.classList.add("drawer-open");
  syncTaskDrawer();                                              // paint the list-driven summary at once…
  fetchTaskDetail(fid);                                          // …then fetch the comment-derived deliverable (on open, like the monitor's /progress fetch)
}
// Fetch the single-feature detail (/features/{fid} → get_feature → br show, which carries
// the comment-derived deliverable the list projection omits) for the open task and re-
// render the drawer with it (#312). This is the ONLY source of the drawer's deliverable —
// the 10s /features poll carries only a preview (#399). Fenced on TASK_FID exactly like pollMonitor:
// a fetch that resolves AFTER the drawer is closed or switched to another task/the monitor
// re-checks TASK_FID on BOTH the success and the error path and bails without writing, so
// a late resolve can't clobber a re-purposed #drawer-body.
//
// TASK_FID alone only fences ACROSS tasks. Two fetches for the SAME open task race each
// other: open the drawer (fetch A), approve (fetch B), and if A resolves after B it
// overwrites B's newer post-action detail with pre-action data. So each fetch also takes
// a monotonic ticket and writes only while it is still the latest — last-issued wins,
// never last-to-resolve. The ticket is PER-FID: a single global counter let a delayed
// action on task A bump the sequence and silently invalidate task B's in-flight fetch
// after the drawer switched, leaving B with no deliverable and no replacement fetch.
async function fetchTaskDetail(fid){
  const seq = TASK_DETAIL_SEQ[fid] = (TASK_DETAIL_SEQ[fid] || 0) + 1;
  try {
    const f = await api(FEAT + encodeURIComponent(fid));
    if (TASK_FID !== fid || seq !== TASK_DETAIL_SEQ[fid]) return;
    TASK_DETAIL = {fid: fid, feature: f};
  } catch (e) {
    if (TASK_FID !== fid || seq !== TASK_DETAIL_SEQ[fid]) return;
    TASK_DETAIL = {fid: fid, error: "" + ((e && e.message) || e)};
  }
  syncTaskDrawer();
}
// Render the open task's detail from the live FEATURES into the shared drawer body — a
// no-op when no task is open, so it's safe to call at the end of every render(). Guarded
// on TASK_FID so it never touches the drawer while it's showing the coder monitor.
function syncTaskDrawer(){
  if (!TASK_FID) return;
  const f = FEATURES.find(x => x.id === TASK_FID);
  if (!f) { $("drawer-body").innerHTML = '<div class="pl-empty">Task not found.</div>'; return; }
  // The list-driven summary (state, spec, controls) comes from FEATURES so a 10s-poll /
  // action re-render still tracks in_progress → in_review → done; the comment-derived
  // deliverable the list omits is spliced in from the single-fetch (TASK_DETAIL) once it
  // has landed for THIS task (#312), and so is the requirement ledger (#399) — the
  // verifier reads the same card read the deliverable came from. A failed single-fetch
  // surfaces as an error callout ABOVE the detail rather than leaving the deliverable
  // silently blank; an approval past open requirements (VERIFY_NOTE) rides the same slot.
  const d = (TASK_DETAIL && TASK_DETAIL.fid === TASK_FID) ? TASK_DETAIL : null;
  const merged = d && d.feature ? {...f, deliverable: d.feature.deliverable, requirements: d.feature.requirements} : f;
  const err = d && d.error
    ? '<div class="pl-callout pl-callout--error">'+esc("Couldn't load task detail: " + d.error)+'</div>'
    : "";
  const note = VERIFY_NOTE && VERIFY_NOTE.fid === TASK_FID
    ? '<div class="pl-callout pl-callout--warning"><b>Approved</b> with '+esc(VERIFY_NOTE.note)+'</div>'
    : "";
  $("drawer-body").innerHTML = err + note + taskDetail(merged);
}
function closeMonitor(){
  MON_FID = null; TASK_FID = null; TASK_DETAIL = null; VERIFY_NOTE = null;
  if (MON_TIMER) { clearInterval(MON_TIMER); MON_TIMER = null; }
  $("drawer").classList.remove("open"); $("scrim").classList.remove("open");
  document.body.classList.remove("drawer-open");
}
// Delegate clicks: a [data-mon] element (coding in_progress card/row) opens the monitor,
// a [data-task] element (any task card/row) opens its detail drawer; the task action
// buttons (#217) are delegated the same way, keyed on data-* verb.
document.addEventListener("click", (e) => {
  const mb = e.target.closest("[data-card-menu]");
  if (mb) { if (MENU_FOR === mb.getAttribute("data-card-menu")) closeMenu(true); else openMenu(mb.getAttribute("data-card-menu"), mb); return; }
  if (e.target.closest("#move-menu")) return;
  const el = e.target.closest("[data-mon]");
  if (el) { openMonitor(el.getAttribute("data-mon")); return; }
  const tel = e.target.closest("[data-task]");
  if (tel) { openTask(tel.getAttribute("data-task")); return; }
  const act = e.target.closest("[data-deliver],[data-approve],[data-reject],[data-reject-toggle]");
  if (!act) return;
  if (act.hasAttribute("data-deliver")) submitDeliver(act.getAttribute("data-deliver"));
  else if (act.hasAttribute("data-approve")) approveTask(act.getAttribute("data-approve"));
  else if (act.hasAttribute("data-reject")) rejectTask(act.getAttribute("data-reject"));
  else if (act.hasAttribute("data-reject-toggle")) toggleReject(act.getAttribute("data-reject-toggle"));
});
$("scrim").addEventListener("click", closeMonitor);              // click-away closes
$("drawer-close").addEventListener("click", closeMonitor);
// Esc unwinds the innermost thing first: a drag (snaps back), the action menu, the drawer.
function escapeLayer(){
  if (DRAG) { endDrag(false); return; }
  if (closeMenu(true)) return;
  closeMonitor();
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") escapeLayer(); });

// Module scripts are scoped — expose the inline onclick handlers (view toggle +
// the list's per-state collapse + the Done cap's "show all").
window.setView = setView;
window.toggleGroup = toggleGroup;
window.showAllDone = showAllDone;
setView(VIEW);   // sync the toggle + visibility to the initial view (list on mobile)
// Boot ONCE, on whichever fires first: the handshake (the bearer arrives with
// protoagent:init, so the gated /features pull authenticates) or a short timer
// for the no-handshake case (standalone page / older host).
//
// Live, not just polled: the board polls the store's change stamp (GET /rev — a few
// `stat`s server-side, no br) every REV_POLL_MS and re-reads /features only when it
// moved, so a card the loop moves shows up within a couple of seconds while an idle board
// costs next to nothing. A full read still runs every FULL_REFRESH_MS (an ETag'd 304 when
// nothing changed) for what the stamp can't see — the CI / review chips come from
// in-process caches, not the store. An older plugin with no /rev falls back to the plain
// 10 s poll. Nothing polls while the page is hidden; coming back refreshes at once.
const REV_POLL_MS = 2000, FULL_REFRESH_MS = 15000, SLOW_POLL_MS = 10000;
let REV = null, REV_OK = true, LIVE_TIMER = null, TICKING = false;
async function liveTick(){
  if (TICKING) return;
  TICKING = true;
  clearTimeout(LIVE_TIMER);
  try {
    if (!document.hidden){
      let changed = false;
      if (REV_OK){
        try {
          const r = await kit.apiFetch("/api/plugins/project_board/rev");
          if (r.status === 404) REV_OK = false;
          else if (r.ok){
            // The first stamp re-reads too: a write between the boot read and this tick
            // would otherwise be baked into the baseline and wait for the full refresh.
            const d = await r.json(); changed = REV === null || d.rev !== REV; REV = d.rev;
          }
        } catch (e) { /* transient — the next tick retries */ }
      }
      if (changed || !REV_OK || Date.now() - LAST_FULL >= FULL_REFRESH_MS) await load();
      if (VIEW === "kanban" && Date.now() - LAST_BUILD_POLL >= BUILD_POLL_MS) await pollBuilding();
    }
  } finally {
    TICKING = false;
    $("live").classList.toggle("live--off", !REV_OK);
    LIVE_TIMER = setTimeout(liveTick, REV_OK ? REV_POLL_MS : SLOW_POLL_MS);
  }
}
// The building strips' elapsed clocks tick locally between /progress reads.
function clockTick(){
  if (!document.hidden && VIEW === "kanban") paintAllBuilding();
  setTimeout(clockTick, 1000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) liveTick(); });
let booted = false;
function boot(){
  if (booted) return;
  booted = true;
  load().then(pollBuilding);
  LIVE_TIMER = setTimeout(liveTick, REV_POLL_MS);
  clockTick();
}
kit.initPluginView(boot);
setTimeout(boot, 800);
