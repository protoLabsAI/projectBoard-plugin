"""Board console view (ADR 0026, D5) — the Kanban/list board UI.

A self-contained page served at ``/plugins/project_board/board`` (by the API
router in api.py — see __init__.register) that renders the board two ways (Kanban
columns = the 6 states, and a dense list), toggled — two projections of the same
features over the already-proven ``/features`` API. The console renders a left-rail
icon (manifest ``views:``) whose panel iframes this page; on load the console
``postMessage``s a bearer token + theme tokens (the ADR 0026 handshake), which the
page applies for its same-origin API calls.

FLEET-PROXY-SAFE (ADR 0042): the iframe loads at /plugins/project_board/board on
the host window, but at /agents/<slug>/plugins/project_board/board when this
agent is viewed through the fleet proxy. So the page derives ``base`` from its own
path (= "" on host, "/agents/<slug>" when proxied) and prefixes EVERY fetch + asset
with it — never hardcode an absolute "/api/...", "/plugins/...", or
"http://localhost:PORT" (that breaks the proxy + the same-origin postMessage token).

No build step — vanilla JS + inline SVG, so the whole plugin stays a drop-in
package. The page reads the board; it never mutates it (mutation stays the loop +
the tools + the API). ``BOARD_PAGE`` is the HTML; api.py serves it on GET /board.
"""

from __future__ import annotations


BOARD_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Board</title>
<style>
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--pl-color-bg);color:var(--pl-color-fg);
    font-family:var(--pl-font-sans);font-size:13px}
  .wrap{max-width:1240px;margin:0 auto;padding:var(--pl-space-4) var(--pl-space-6)}
  .top{display:flex;align-items:center;gap:var(--pl-space-3);margin-bottom:var(--pl-space-4)}
  h1{font-size:17px;margin:0;color:var(--pl-color-accent);letter-spacing:.2px}
  .sub{color:var(--pl-color-fg-muted);font-size:12px;margin:0;flex:1}
  /* Kanban — no DS primitive for cards/columns; token-driven. */
  .board{display:grid;grid-template-columns:repeat(5,1fr);gap:var(--pl-space-3)}
  .col{background:var(--pl-color-bg-raised);border:var(--pl-border-width) solid var(--pl-color-border);
    border-radius:var(--pl-radius);padding:var(--pl-space-2);min-height:120px}
  .col .pl-panel-header{padding:2px 4px var(--pl-space-2);text-transform:uppercase;
    letter-spacing:.06em;font-size:11px}
  .card{background:var(--pl-color-bg-raised);border:var(--pl-border-width) solid var(--pl-color-border);
    border-left:3px solid var(--pl-color-accent);
    border-radius:var(--pl-radius);padding:9px 10px;margin-bottom:var(--pl-space-2)}
  .card .t{font-size:12.5px;line-height:1.35;margin-bottom:6px}
  .card .m{display:flex;gap:var(--pl-space-2);align-items:center;flex-wrap:wrap;font-size:10.5px;color:var(--pl-color-fg-muted)}
  .id{font-family:var(--pl-font-mono);font-size:10px}
  a.pr{color:var(--pl-color-status-info);text-decoration:none}
  a.pr:hover{text-decoration:underline}
  .hide{display:none}
  /* List view — collapsible per-state group headers (#26). Token-driven, mirrors the
     Kanban column grouping as sections in the dense list. */
  #list tr.grp{cursor:pointer;user-select:none}
  #list tr.grp>td{background:var(--pl-color-bg-raised);text-transform:uppercase;
    letter-spacing:.06em;font-size:11px;font-weight:600;color:var(--pl-color-fg-muted);
    padding:var(--pl-space-2) 4px}
  #list tr.grp:hover>td{color:var(--pl-color-fg)}
  #list tr.grp .tw{display:inline-block;width:1.1em;text-align:center}
  #list tr.grp .gl{margin-right:var(--pl-space-2)}
  /* in_progress cards/rows open the live monitor drawer (#84) — cue it's clickable. */
  .card--live{cursor:pointer}
  .card--live:hover{border-color:var(--pl-color-accent)}
  #list tr[data-mon]{cursor:pointer}
  /* ── Live monitor drawer (#84): a slide-over mirroring the console goal-detail-drawer
     UX — a right-edge panel over a click-away scrim, polling the per-feature progress
     snapshot while open. Vanilla + token-driven; this page is an iframe, so it owns its
     own HTML/JS and imports NO console components. */
  #scrim{position:fixed;inset:0;background:rgba(0,0,0,.45);opacity:0;pointer-events:none;
    transition:opacity .15s ease;z-index:40}
  #scrim.open{opacity:1;pointer-events:auto}
  #drawer{position:fixed;top:0;right:0;height:100%;width:min(460px,92vw);z-index:41;
    background:var(--pl-color-bg-raised);border-left:var(--pl-border-width) solid var(--pl-color-border);
    box-shadow:-8px 0 24px rgba(0,0,0,.25);transform:translateX(100%);transition:transform .18s ease;
    display:flex;flex-direction:column;overflow:hidden}
  #drawer.open{transform:translateX(0)}
  #drawer .dh{display:flex;align-items:center;gap:var(--pl-space-2);padding:var(--pl-space-3) var(--pl-space-4);
    border-bottom:var(--pl-border-width) solid var(--pl-color-border)}
  #drawer .dh h2{font-size:14px;margin:0;flex:1;color:var(--pl-color-accent)}
  #drawer .dx{cursor:pointer;background:none;border:none;color:var(--pl-color-fg-muted);font-size:18px;line-height:1}
  #drawer .dx:hover{color:var(--pl-color-fg)}
  #drawer .db{padding:var(--pl-space-3) var(--pl-space-4);overflow:auto;flex:1}
  .gen{border:var(--pl-border-width) solid var(--pl-color-border);border-radius:var(--pl-radius);
    padding:var(--pl-space-3);margin-bottom:var(--pl-space-3)}
  .gen .gh{display:flex;align-items:center;gap:var(--pl-space-2);margin-bottom:var(--pl-space-2);flex-wrap:wrap}
  .gen .gh .gn{font-weight:600}
  .gen .lbl{text-transform:uppercase;letter-spacing:.06em;font-size:10px;color:var(--pl-color-fg-muted);
    margin:var(--pl-space-2) 0 2px}
  .gen .cur{font-family:var(--pl-font-mono);font-size:11.5px;word-break:break-word}
  .gen .loc{color:var(--pl-color-fg-muted);font-family:var(--pl-font-mono);font-size:10.5px}
  .gen .thought{white-space:pre-wrap;word-break:break-word;font-size:11.5px;color:var(--pl-color-fg-muted);
    max-height:120px;overflow:auto}
  .gen ul.tools{list-style:none;margin:0;padding:0;max-height:150px;overflow:auto}
  .gen ul.tools li{font-family:var(--pl-font-mono);font-size:10.5px;padding:1px 0;color:var(--pl-color-fg-muted)}
  .st-completed{color:var(--pl-color-status-success)}
  .st-failed{color:var(--pl-color-status-error)}
  .st-running{color:var(--pl-color-accent)}
  .st-start{color:var(--pl-color-fg-muted)}
  /* Narrow/mobile: the JS auto-switches to the list; if Kanban is forced, stack it. */
  @media (max-width:760px){ .board{grid-template-columns:1fr} .wrap{padding:var(--pl-space-3)} }
</style>
<script>
// ── Slug-aware base (ADR 0042): "" on the host window, "/agents/<slug>" when proxied.
// Split on the prefix this page is served under so EVERY asset + fetch stays
// same-origin and reaches THIS agent (never the host) through the fleet proxy.
var BASE = location.pathname.split("/plugins/")[0];
// Link the DS kit same-origin off BASE (rule 4) — --pl-* tokens, never hardcode hex.
(function(){ var l=document.createElement("link"); l.rel="stylesheet";
  l.href=BASE+"/_ds/plugin-kit.css"; document.head.appendChild(l); })();
</script>
</head><body><div class="wrap">
  <div class="top">
    <h1>Board</h1>
    <p class="sub" id="sub">project_board — a projection over beads</p>
    <div class="pl-tabs">
      <button id="tk" class="pl-tab pl-tab--active" onclick="setView('kanban')">Kanban</button>
      <button id="tl" class="pl-tab" onclick="setView('list')">List</button>
    </div>
  </div>
  <div id="err" class="pl-callout pl-callout--error" hidden></div>
  <div id="kanban" class="board"></div>
  <table id="list" class="pl-table hide"><thead><tr>
    <th>ID</th><th>Title</th><th>State</th><th>Pri</th><th>Flags</th><th>PR</th></tr></thead>
    <tbody id="rows"></tbody></table>
</div>
<!-- Live monitor drawer (#84): a slide-over over a click-away scrim. -->
<div id="scrim"></div>
<div id="drawer" role="dialog" aria-modal="true" aria-label="Coder monitor">
  <div class="dh"><h2 id="drawer-title">Coder monitor</h2>
    <button class="dx" id="drawer-close" aria-label="Close">✕</button></div>
  <div class="db" id="drawer-body"></div>
</div>
<script type="module">
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
function flags(f){
  let out = "";
  if (f.blocked) out += '<span class="pl-badge pl-badge--error">blocked</span>';
  if (f.dag_blocked) out += '<span class="pl-badge pl-badge--warning">waiting on deps</span>';
  if (f.difficulty) out += '<span class="pl-badge">'+esc(f.difficulty)+'</span>';
  return out;
}
function pr(f){ return f.pr_url ? '<a class="pr" href="'+esc(f.pr_url)+'" target="_blank">PR ↗</a>' : ""; }

// State → DS dot variant (for the list view chip).
const DOT_VARIANT = {ready:"pl-dot--success", in_review:"pl-dot--info", blocked:"pl-dot--error"};

// List view sections: the Kanban columns + the `blocked` flag-state + `cancelled`
// (the second terminal edge, #47), rendered as collapsible groups in COLS order (#26).
const LIST_SECTIONS = [...COLS, "blocked", "cancelled"];
// States the user has collapsed — module-scoped so the 10s auto-reload re-render
// doesn't re-expand what they closed.
const COLLAPSED = new Set();
function toggleGroup(state){ COLLAPSED.has(state) ? COLLAPSED.delete(state) : COLLAPSED.add(state); render(); }

function render(){
  if (VIEW === "kanban"){
    $("kanban").innerHTML = COLS.map(state => {
      const items = FEATURES.filter(f => f.state === state);
      const cards = items.map(f => {
        const color = f.blocked ? "var(--pl-color-status-error)" : (f.dag_blocked ? "var(--pl-color-status-warning)" : (STATE_COLOR[state]||"var(--pl-color-accent)"));
        // An in_progress card is live — clicking it opens the coder monitor drawer (#84).
        const live = state === "in_progress";
        return '<div class="card'+(live?" card--live":"")+'"'+(live?' data-mon="'+esc(f.id)+'"':"")
          + ' style="border-left-color:'+color+'">'
          + '<div class="t">'+esc(f.title)+'</div>'
          + '<div class="m"><span class="id">'+esc(f.id)+'</span><span>P'+f.priority+'</span>'
          + flags(f)+' '+pr(f)+'</div></div>';
      }).join("") || '<div class="pl-empty">—</div>';
      return '<div class="col"><div class="pl-panel-header pl-panel-header--compact">'
        + '<span class="pl-panel-header__title">'+state.replace("_"," ")+'</span>'
        + '<span class="pl-badge">'+items.length+'</span></div>'+cards+'</div>';
    }).join("");
  } else {
    // List: group rows under a collapsible per-state header (COLS order + blocked +
    // cancelled), mirroring the Kanban's grouping so a dense board stays scannable (#26).
    const row = (f) =>
      '<tr'+(f.state==="in_progress"?' data-mon="'+esc(f.id)+'"':"")+'>'  // in_progress → opens the monitor (#84)
      + '<td class="id">'+esc(f.id)+'</td><td>'+esc(f.title)+'</td>'
      + '<td><span class="pl-dot-row"><span class="pl-dot '+(DOT_VARIANT[f.state]||"")+'"></span>'
      + '<span class="pl-dot-row__label">'+esc(f.state)+'</span></span></td>'
      + '<td>P'+f.priority+'</td><td>'+flags(f)+'</td><td>'+pr(f)+'</td></tr>';
    const byState = {};
    FEATURES.forEach(f => (byState[f.state] = byState[f.state] || []).push(f));
    // COLS order + blocked + cancelled; any unexpected state lands in its own group last.
    const order = LIST_SECTIONS.slice();
    Object.keys(byState).forEach(s => { if (!order.includes(s)) order.push(s); });
    let html = "";
    order.forEach(state => {
      const items = byState[state] || [];
      if (!items.length) return;  // omit empty sections
      const collapsed = COLLAPSED.has(state);
      html += '<tr class="grp" data-state="'+esc(state)+'" onclick="toggleGroup(this.dataset.state)">'
        + '<td colspan="6"><span class="tw">'+(collapsed?"▸":"▾")+'</span>'
        + '<span class="gl">'+esc(state.replace("_"," "))+'</span>'
        + '<span class="pl-badge">'+items.length+'</span></td></tr>';
      if (!collapsed) html += items.map(row).join("");  // collapsed → header only (rows omitted)
    });
    $("rows").innerHTML = html || '<tr><td colspan="6"><div class="pl-empty">No features yet — create some via the board tools or API.</div></td></tr>';
  }
}

async function load(){
  try {
    const r = await api("/api/plugins/project_board/features");
    // the /features API field is `board_state`; normalize to `state` for the views.
    FEATURES = (r.features || []).map(f => ({...f, state: f.board_state ?? f.state}))
      .sort((a,b) => a.priority - b.priority || a.id.localeCompare(b.id));
    $("err").hidden = true;
    $("sub").textContent = "project_board — " + FEATURES.length + " features · a projection over beads";
    render();
  } catch (e) {
    $("err").hidden = false; $("err").textContent = "Could not load the board: " + e;
  }
}

// ── Live coder monitor drawer (#84) ────────────────────────────────────────────
// Clicking an in_progress card/row opens a right-edge slide-over that polls the
// per-feature progress snapshot every ~3s, mirroring the console's goal-detail-
// drawer UX in this page's OWN vanilla HTML/JS (an iframe — no console imports).
const MON_POLL_MS = 3000;
let MON_FID = null, MON_TIMER = null;

function toolLine(t){
  const st = esc(t.status||"");
  const loc = (t.locations && t.locations.length) ? " <span class=\"loc\">"+esc(t.locations.join(", "))+"</span>" : "";
  return '<li><span class="st-'+st+'">'+st+'</span> '+esc(t.name||"tool")
    + (t.kind?' <span class="loc">['+esc(t.kind)+']</span>':"")+loc+'</li>';
}
function genCard(g){
  let h = '<div class="gen"><div class="gh"><span class="gn">gen '+esc(String(g.gen))+'</span>'
    + (g.tier?'<span class="pl-badge">'+esc(g.tier)+'</span>':"")
    + '<span class="pl-badge">'+esc(String(g.elapsed_s))+'s</span>'
    + (g.usage?'<span class="pl-badge">'+esc(String(g.usage.used))+'/'+esc(String(g.usage.size))+' tok</span>':"")
    + '</div>';
  const cur = g.current_tool;
  h += '<div class="lbl">current tool</div><div class="cur">'
    + (cur ? '<span class="st-'+esc(cur.status||"")+'">'+esc(cur.status||"")+'</span> '+esc(cur.name||"")
        + (cur.locations&&cur.locations.length?' <span class="loc">'+esc(cur.locations.join(", "))+'</span>':"")
       : "—") + '</div>';
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
}
async function pollMonitor(){
  if (!MON_FID) return;
  try { renderMonitor(await api("/api/plugins/project_board/features/"+encodeURIComponent(MON_FID)+"/progress")); }
  catch (e) { $("drawer-body").innerHTML = '<div class="pl-callout pl-callout--error">'+esc(""+e)+'</div>'; }
}
function openMonitor(fid){
  MON_FID = fid;
  $("drawer-title").textContent = "Coder monitor — " + fid;
  $("drawer").classList.add("open"); $("scrim").classList.add("open");
  $("drawer-body").innerHTML = '<div class="pl-empty">Loading…</div>';
  pollMonitor();
  if (MON_TIMER) clearInterval(MON_TIMER);
  MON_TIMER = setInterval(pollMonitor, MON_POLL_MS);
}
function closeMonitor(){
  MON_FID = null;
  if (MON_TIMER) { clearInterval(MON_TIMER); MON_TIMER = null; }
  $("drawer").classList.remove("open"); $("scrim").classList.remove("open");
}
// Delegate clicks: any [data-mon] element (in_progress card or row) opens the drawer.
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-mon]");
  if (el) openMonitor(el.getAttribute("data-mon"));
});
$("scrim").addEventListener("click", closeMonitor);              // click-away closes
$("drawer-close").addEventListener("click", closeMonitor);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMonitor(); });  // Esc closes

// Module scripts are scoped — expose the inline onclick handlers (view toggle +
// the list's per-state collapse).
window.setView = setView;
window.toggleGroup = toggleGroup;
setView(VIEW);   // sync the toggle + visibility to the initial view (list on mobile)
// Boot ONCE, on whichever fires first: the handshake (the bearer arrives with
// protoagent:init, so the gated /features pull authenticates) or a short timer
// for the no-handshake case (standalone page / older host).
let booted = false;
function boot(){ if (booted) return; booted = true; load(); setInterval(load, 10000); }
kit.initPluginView(boot);
setTimeout(boot, 800);
</script></body></html>"""
