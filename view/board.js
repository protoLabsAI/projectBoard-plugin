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
// State → DS status token. (blocked → error, dag/deps → warning handled in flags.)
const STATE_COLOR = {backlog:"var(--pl-color-fg-muted)", ready:"var(--pl-color-status-success)",
  in_progress:"var(--pl-color-accent)", in_review:"var(--pl-color-status-info)",
  done:"var(--pl-color-fg-muted)", blocked:"var(--pl-color-status-error)"};
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
};
function nextActionChip(f){
  if (!f.next_action || f.next_action === "blocked") return "";  // blocked has its own chip
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
// The task-detail drawer body (bd-rdmh): a task's detail + controls live in the SHARED
// #drawer instead of inline on the Kanban card / list row. Renders the spec, the
// acceptance criteria, any recorded deliverable, then the state-appropriate controls
// (taskExtra). Everything is esc()'d — server-authored text, never raw HTML.
function taskDetail(f){
  const prose = (s) => s ? '<div class="tdsec">'+esc(s)+'</div>' : '<div class="tdsec tdsec--empty">—</div>';
  let h = '<div class="tdlbl">spec</div>' + prose(f.spec)
    + '<div class="tdlbl">acceptance criteria</div>' + prose(f.acceptance_criteria);
  if (f.deliverable) h += '<div class="tdlbl">deliverable</div><div class="deliv">'+esc(f.deliverable)+'</div>';
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
  // deliverable (the list reload above can't — /features omits comments; see #312).
  try { await apiPost(FEAT+encodeURIComponent(fid)+"/deliver", {text: text, ref: ref}); await load(); await fetchTaskDetail(fid); }
  catch (e) { taskErr(fid, e); }
}
async function approveTask(fid){
  try { await apiPost(FEAT+encodeURIComponent(fid)+"/verify", {approved: true}); await load(); await fetchTaskDetail(fid); }
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
  if (VIEW === "kanban"){
    $("kanban").innerHTML = COLS.map(state => {
      let items = FEATURES.filter(f => f.state === state), total = items.length;
      if (state === "done"){ const d = doneSlice(items); items = d.items; total = d.total; }  // cap Done (#115)
      const cards = items.map(f => {
        const color = f.blocked ? "var(--pl-color-status-error)" : (f.dag_blocked ? "var(--pl-color-status-warning)" : (STATE_COLOR[state]||"var(--pl-color-accent)"));
        // An in_progress card is live — clicking it opens the coder monitor drawer (#84).
        // A task never dispatches a coder, so it never opens the MONITOR (#217); it opens
        // the same drawer showing its detail + controls (bd-rdmh) via a data-task handle,
        // with no polling. The card itself is summary-only — no inline form/controls.
        const live = state === "in_progress";
        const mon = live && !isTask(f);
        return '<div class="card'+(mon?" card--live":"")+(isTask(f)?" card--task":"")+'"'
          + (mon?' data-mon="'+esc(f.id)+'"':"")+(isTask(f)?' data-task="'+esc(f.id)+'"':"")
          + ' style="border-left-color:'+color+'">'
          + '<div class="t">'+docIco(f)+esc(f.title)+'</div>'
          + '<div class="m"><span class="id">'+esc(f.id)+'</span><span>P'+f.priority+'</span>'
          + flags(f)+' '+taskFoot(f)+'</div></div>';
      }).join("") || '<div class="pl-empty">—</div>';
      const more = total > items.length ? showAllBtn(total) : "";
      return '<div class="col"><div class="pl-panel-header pl-panel-header--compact">'
        + '<span class="pl-panel-header__title">'+(STATE_LABEL[state] || state.replace("_"," "))+'</span>'
        + '<span class="pl-badge">'+total+'</span></div>'+cards+more+'</div>';
    }).join("");
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

async function load(){
  try {
    const r = await api("/api/plugins/project_board/features");
    // the /features API field is `board_state`; normalize to `state` for the views.
    // blocked floats first (#201), in_progress second (#223) — matching list_features —
    // then priority asc, id tiebreak.
    FEATURES = (r.features || []).map(f => ({...f, state: f.board_state ?? f.state}))
      .sort((a,b) => (a.blocked?0:a.state==='in_progress'?1:2) - (b.blocked?0:b.state==='in_progress'?1:2) || a.priority - b.priority || a.id.localeCompare(b.id));
    $("sub").textContent = "project_board — " + FEATURES.length + " features · a projection over beads";
    render();
    // The board READS fine but may still be unable to RUN (no coder, no gh, br
    // vanished): the setup preflight on /status says which check fails and how
    // to fix it — a warning card above the board, never a silent green.
    const s = await api("/api/plugins/project_board/status").catch(() => null);
    // br fetched on first run (v0.43.0): say so in the subtitle, once it's the store's binary.
    if (s && s.setup && s.setup.br && s.setup.br.source === "fetched") $("sub").textContent += " · br v" + (s.setup.br.fetch.version || "?") + " fetched to " + s.setup.br.path;
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
let MON_FID = null, MON_TIMER = null, TASK_FID = null;
// The single-card detail behind the OPEN task drawer (#312): the list projection the 10s
// poll pulls (/features → br list) intentionally OMITS bead comments, so its comment-
// derived `deliverable` field is always "" — the drawer would render blank for a delivered
// task. So the drawer fetches the single-feature route (/features/{fid} → get_feature → br
// show, which carries comments) ON OPEN and after each action, caching the result here so
// the 10s poll's syncTaskDrawer re-render reuses it (no per-task br show every tick, the
// monitor's on-open /progress posture). Shape: {fid, feature} on success, {fid, error} on
// a failed fetch (surfaced in the drawer); null when no task is open / not yet fetched.
let TASK_DETAIL = null;
// Monotonic ticket for fetchTaskDetail — see its comment: fences two in-flight
// fetches of the SAME task so a slow earlier one cannot overwrite a newer one.
const TASK_DETAIL_SEQ = {};

function toolLine(t){
  const st = esc(t.status||"");
  const loc = (t.locations && t.locations.length) ? " <span class=\"loc\">"+esc(t.locations.join(", "))+"</span>" : "";
  return '<li><span class="st-'+st+'">'+st+'</span> '+esc(t.name||"tool")
    + (t.kind?' <span class="loc">['+esc(t.kind)+']</span>':"")+loc+'</li>';
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
function genCard(g){
  let h = '<div class="gen"><div class="gh"><span class="gn">gen '+esc(String(g.gen))+'</span>'
    + (g.tier?'<span class="pl-badge">'+esc(g.tier)+'</span>':"")
    + '<span class="pl-badge">'+esc(String(g.elapsed_s))+'s</span>'
    + (g.usage?'<span class="pl-badge">'+esc(String(g.usage.used))+'/'+esc(String(g.usage.size))+' tok</span>':"")
    + '</div>';
  // The coder's own plan (ACP plan updates — its live todo list): the sharpest
  // "where is it in the work" signal, rendered as a status-glyphed checklist.
  if (g.plan && g.plan.length){
    h += '<div class="lbl">plan</div><ul class="plan">'
      + g.plan.map(e => {
          const st = e.status||"";
          const glyph = st==="completed" ? "✓" : (st==="in_progress" ? "▸" : "·");
          return '<li class="pl-'+esc(st)+'"><span class="glyph">'+glyph+'</span> '+esc(e.content||"")+'</li>';
        }).join("") + '</ul>';
  }
  const cur = g.current_tool;
  h += '<div class="lbl">current tool</div><div class="cur">'
    + (cur ? '<span class="st-'+esc(cur.status||"")+'">'+esc(cur.status||"")+'</span> '+esc(cur.name||"")
        + (cur.locations&&cur.locations.length?' <span class="loc">'+esc(cur.locations.join(", "))+'</span>':"")
        + (cur.input_preview?'<div class="inprev">'+esc(cur.input_preview)+'</div>':"")
       : "—") + '</div>';
  // "saying" is markdown — carry the raw source on data-md and render esc()'d text inline
  // as the fallback; enhanceSaying() upgrades it to rendered markdown once marked loads.
  // The .thought class is kept so it inherits the drawer's scroll/overflow cap + lone-gen
  // fill (#218/#226-UX); .md-saying is the enhancement hook.
  if (g.answer_tail){ h += '<div class="lbl">saying</div><div class="thought md-saying" data-md="'+esc(g.answer_tail)+'">'+esc(g.answer_tail)+'</div>'; }
  // "thinking" stays plain esc()'d text — internal reasoning, not user-facing prose.
  if (g.thought_tail){ h += '<div class="lbl">thinking</div><div class="thought">'+esc(g.thought_tail)+'</div>'; }
  const rt = (g.recent_tools||[]).slice(-30).reverse();
  if (rt.length){ h += '<div class="lbl">recent tools</div><ul class="tools">'+rt.map(toolLine).join("")+'</ul>'; }
  if (g.verify){ h += '<div class="lbl">verify</div><div class="cur"><span class="st-'
    + (g.verify.passed?"completed":"failed")+'">'+(g.verify.passed?"passed":"failed")+'</span> '
    + esc(g.verify.test_cmd||"")+'</div>'; }
  return h + '</div>';
}
function renderMonitor(data){
  const gens = (data && data.gens) || [];
  $("drawer-body").innerHTML = gens.length
    ? gens.map(genCard).join("")
    : '<div class="pl-empty">No live coder run for this feature right now.</div>';
  enhanceSaying($("drawer-body"));   // upgrade "saying" plain text → rendered markdown (lazy, best-effort)
}
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
  $("drawer-title").textContent = "Task — " + fid;
  $("drawer").classList.add("open"); $("scrim").classList.add("open");
  document.body.classList.add("drawer-open");
  syncTaskDrawer();                                              // paint the list-driven summary at once…
  fetchTaskDetail(fid);                                          // …then fetch the comment-derived deliverable (on open, like the monitor's /progress fetch)
}
// Fetch the single-feature detail (/features/{fid} → get_feature → br show, which carries
// the comment-derived deliverable the list projection omits) for the open task and re-
// render the drawer with it (#312). This is the ONLY source of the drawer's deliverable —
// the 10s /features poll never carries one. Fenced on TASK_FID exactly like pollMonitor:
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
  // has landed for THIS task (#312). A failed single-fetch surfaces as an error callout
  // ABOVE the detail rather than leaving the deliverable silently blank.
  const d = (TASK_DETAIL && TASK_DETAIL.fid === TASK_FID) ? TASK_DETAIL : null;
  const merged = d && d.feature ? {...f, deliverable: d.feature.deliverable} : f;
  const err = d && d.error
    ? '<div class="pl-callout pl-callout--error">'+esc("Couldn't load task detail: " + d.error)+'</div>'
    : "";
  $("drawer-body").innerHTML = err + taskDetail(merged);
}
function closeMonitor(){
  MON_FID = null; TASK_FID = null; TASK_DETAIL = null;
  if (MON_TIMER) { clearInterval(MON_TIMER); MON_TIMER = null; }
  $("drawer").classList.remove("open"); $("scrim").classList.remove("open");
  document.body.classList.remove("drawer-open");
}
// Delegate clicks: a [data-mon] element (coding in_progress card/row) opens the monitor,
// a [data-task] element (any task card/row) opens its detail drawer; the task action
// buttons (#217) are delegated the same way, keyed on data-* verb.
document.addEventListener("click", (e) => {
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
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMonitor(); });  // Esc closes

// Module scripts are scoped — expose the inline onclick handlers (view toggle +
// the list's per-state collapse + the Done cap's "show all").
window.setView = setView;
window.toggleGroup = toggleGroup;
window.showAllDone = showAllDone;
setView(VIEW);   // sync the toggle + visibility to the initial view (list on mobile)
// Boot ONCE, on whichever fires first: the handshake (the bearer arrives with
// protoagent:init, so the gated /features pull authenticates) or a short timer
// for the no-handshake case (standalone page / older host).
let booted = false;
function boot(){ if (booted) return; booted = true; load(); setInterval(load, 10000); }
kit.initPluginView(boot);
setTimeout(boot, 800);
