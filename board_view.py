"""Board console view (ADR 0026, D5) — the deferred-until-now UI.

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
const api = (p) => kit.apiFetch(p).then(r => r.json());
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

function render(){
  if (VIEW === "kanban"){
    $("kanban").innerHTML = COLS.map(state => {
      const items = FEATURES.filter(f => f.state === state);
      const cards = items.map(f => {
        const color = f.blocked ? "var(--pl-color-status-error)" : (f.dag_blocked ? "var(--pl-color-status-warning)" : (STATE_COLOR[state]||"var(--pl-color-accent)"));
        return '<div class="card" style="border-left-color:'+color+'">'
          + '<div class="t">'+esc(f.title)+'</div>'
          + '<div class="m"><span class="id">'+esc(f.id)+'</span><span>P'+f.priority+'</span>'
          + flags(f)+' '+pr(f)+'</div></div>';
      }).join("") || '<div class="pl-empty">—</div>';
      return '<div class="col"><div class="pl-panel-header pl-panel-header--compact">'
        + '<span class="pl-panel-header__title">'+state.replace("_"," ")+'</span>'
        + '<span class="pl-badge">'+items.length+'</span></div>'+cards+'</div>';
    }).join("");
  } else {
    $("rows").innerHTML = FEATURES.map(f =>
      '<tr><td class="id">'+esc(f.id)+'</td><td>'+esc(f.title)+'</td>'
      + '<td><span class="pl-dot-row"><span class="pl-dot '+(DOT_VARIANT[f.state]||"")+'"></span>'
      + '<span class="pl-dot-row__label">'+esc(f.state)+'</span></span></td>'
      + '<td>P'+f.priority+'</td><td>'+flags(f)+'</td><td>'+pr(f)+'</td></tr>'
    ).join("") || '<tr><td colspan="6"><div class="pl-empty">No features yet — create some via the board tools or API.</div></td></tr>';
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

// Module scripts are scoped — expose the view-toggle's inline onclick handlers.
window.setView = setView;
setView(VIEW);   // sync the toggle + visibility to the initial view (list on mobile)
// Boot ONCE, on whichever fires first: the handshake (the bearer arrives with
// protoagent:init, so the gated /features pull authenticates) or a short timer
// for the no-handshake case (standalone page / older host).
let booted = false;
function boot(){ if (booted) return; booted = true; load(); setInterval(load, 10000); }
kit.initPluginView(boot);
setTimeout(boot, 800);
</script></body></html>"""
