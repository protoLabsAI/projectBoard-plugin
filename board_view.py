"""Board console view (ADR 0026, D5) — the deferred-until-now UI.

A self-contained page served at ``/plugins/project_board/board`` that renders the
board two ways (Kanban columns = the 6 states, and a dense list), toggled — two
projections of the same features over the already-proven ``/features`` API. The
console renders a left-rail icon (manifest ``views:``) whose panel iframes this
page; on load the console ``postMessage``s a bearer token + theme tokens (the ADR
0026 handshake), which the page applies for its same-origin API calls.

No build step — vanilla JS + inline SVG, so the whole plugin stays a drop-in
package. The page reads the board; it never mutates it (mutation stays the loop +
the tools + the API).
"""

from __future__ import annotations


def build_board_view_router(cfg: dict | None):
    """A FastAPI router for the board page. The data comes from the existing
    ``/plugins/project_board/features`` endpoint (api.py) — this only serves HTML."""
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/board")
    async def _board():
        return HTMLResponse(_PAGE)

    return router


_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Board</title>
<style>
  :root{
    --bg:#0a0a0c; --raised:#141418; --card:#1a1a20; --border:#26262d; --fg:#ededed;
    --fg-muted:#8b8b95; --accent:#a78bfa; --ready:#46c46a; --review:#5b9dff;
    --done:#6b6b75; --blocked:#e0533a; --dag:#d9a441;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
    font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;font-size:13px}
  .wrap{max-width:1240px;margin:0 auto;padding:18px 22px}
  .top{display:flex;align-items:center;gap:14px;margin-bottom:16px}
  h1{font-size:17px;margin:0;color:var(--accent);letter-spacing:.2px}
  .sub{color:var(--fg-muted);font-size:12px;margin:0;flex:1}
  .toggle{display:flex;border:1px solid var(--border);border-radius:8px;overflow:hidden}
  .toggle button{background:var(--raised);color:var(--fg-muted);border:0;padding:6px 14px;
    font-size:12px;cursor:pointer}
  .toggle button.on{background:var(--accent);color:#0a0a0c;font-weight:600}
  .err{background:rgba(224,83,58,.12);border:1px solid rgba(224,83,58,.4);color:#f0a090;
    border-radius:10px;padding:12px 14px;font-size:13px;margin-bottom:14px}
  /* Kanban */
  .board{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}
  .col{background:var(--raised);border:1px solid var(--border);border-radius:12px;
    padding:10px;min-height:120px}
  .colh{display:flex;justify-content:space-between;align-items:center;margin:2px 4px 10px;
    font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--fg-muted)}
  .count{background:var(--card);border-radius:20px;padding:1px 8px;font-size:11px;color:var(--fg)}
  .card{background:var(--card);border:1px solid var(--border);border-left:3px solid var(--accent);
    border-radius:9px;padding:9px 10px;margin-bottom:8px}
  .card .t{font-size:12.5px;line-height:1.35;margin-bottom:6px}
  .card .m{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:10.5px;color:var(--fg-muted)}
  .id{font-family:ui-monospace,monospace;font-size:10px}
  .pill{border-radius:20px;padding:1px 7px;font-size:10px;font-weight:600}
  .p-blocked{background:rgba(224,83,58,.18);color:#f0a090}
  .p-dag{background:rgba(217,164,65,.16);color:#e8c987}
  .p-diff{background:var(--raised);color:var(--fg-muted)}
  a.pr{color:var(--review);text-decoration:none}
  a.pr:hover{text-decoration:underline}
  /* list */
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--border);font-size:12px}
  th{color:var(--fg-muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.05em}
  .st{font-size:10.5px;padding:1px 8px;border-radius:20px;font-weight:600}
  .empty{color:var(--fg-muted);font-size:11.5px;padding:18px;text-align:center}
  .hide{display:none}
  /* Narrow/mobile: the JS auto-switches to the list; if Kanban is forced, stack it. */
  @media (max-width:760px){ .board{grid-template-columns:1fr} .wrap{padding:14px 14px} }
</style></head><body><div class="wrap">
  <div class="top">
    <h1>Board</h1>
    <p class="sub" id="sub">project_board — a projection over beads</p>
    <div class="toggle">
      <button id="tk" class="on" onclick="setView('kanban')">Kanban</button>
      <button id="tl" onclick="setView('list')">List</button>
    </div>
  </div>
  <div id="err" class="err" hidden></div>
  <div id="kanban" class="board"></div>
  <table id="list" class="hide"><thead><tr>
    <th>ID</th><th>Title</th><th>State</th><th>Pri</th><th>Flags</th><th>PR</th></tr></thead>
    <tbody id="rows"></tbody></table>
</div>
<script>
// ── ADR 0026 handshake: bearer token + theme tokens from the console.
let TOKEN = null;
window.addEventListener("message", (e) => {
  const d = e.data || {};
  if (d.type === "protoagent:init") {
    if (d.token) TOKEN = d.token;
    if (d.theme) for (const [k, v] of Object.entries(d.theme)) {
      if (k.includes("bg")) document.documentElement.style.setProperty("--bg", v);
      if (k.includes("accent")) document.documentElement.style.setProperty("--accent", v);
    }
    load();
  }
});

const COLS = ["backlog", "ready", "in_progress", "in_review", "done"];
const STATE_COLOR = {backlog:"#8b8b95", ready:"var(--ready)", in_progress:"var(--accent)",
  in_review:"var(--review)", done:"var(--done)", blocked:"var(--blocked)"};
const api = (p) => fetch(p, TOKEN ? {headers:{Authorization:"Bearer "+TOKEN}} : {}).then(r => r.json());
const $ = (id) => document.getElementById(id);
const esc = (s) => (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// Narrow viewport → the 5-column Kanban doesn't fit, so default to the list.
const NARROW = window.matchMedia("(max-width: 760px)");
let VIEW = NARROW.matches ? "list" : "kanban";
function setView(v){ VIEW=v; $("tk").classList.toggle("on",v==="kanban"); $("tl").classList.toggle("on",v==="list");
  $("kanban").classList.toggle("hide",v!=="kanban"); $("list").classList.toggle("hide",v!=="list"); render(); }
// Auto-switch to list when the viewport narrows (e.g. rotating a phone / resizing).
NARROW.addEventListener("change", (e) => { if (e.matches && VIEW !== "list") setView("list"); });

let FEATURES = [];
function flags(f){
  let out = "";
  if (f.blocked) out += '<span class="pill p-blocked">blocked</span>';
  if (f.dag_blocked) out += '<span class="pill p-dag">waiting on deps</span>';
  if (f.difficulty) out += '<span class="pill p-diff">'+esc(f.difficulty)+'</span>';
  return out;
}
function pr(f){ return f.pr_url ? '<a class="pr" href="'+esc(f.pr_url)+'" target="_blank">PR ↗</a>' : ""; }

function render(){
  if (VIEW === "kanban"){
    $("kanban").innerHTML = COLS.map(state => {
      const items = FEATURES.filter(f => f.state === state);
      const cards = items.map(f => {
        const color = f.blocked ? "var(--blocked)" : (f.dag_blocked ? "var(--dag)" : (STATE_COLOR[state]||"var(--accent)"));
        return '<div class="card" style="border-left-color:'+color+'">'
          + '<div class="t">'+esc(f.title)+'</div>'
          + '<div class="m"><span class="id">'+esc(f.id)+'</span><span>P'+f.priority+'</span>'
          + flags(f)+' '+pr(f)+'</div></div>';
      }).join("") || '<div class="empty">—</div>';
      return '<div class="col"><div class="colh"><span>'+state.replace("_"," ")+'</span>'
        + '<span class="count">'+items.length+'</span></div>'+cards+'</div>';
    }).join("");
  } else {
    $("rows").innerHTML = FEATURES.map(f =>
      '<tr><td class="id">'+esc(f.id)+'</td><td>'+esc(f.title)+'</td>'
      + '<td><span class="st" style="background:rgba(167,139,250,.14);color:'+(STATE_COLOR[f.state]||"var(--fg)")+'">'+f.state+'</span></td>'
      + '<td>P'+f.priority+'</td><td>'+flags(f)+'</td><td>'+pr(f)+'</td></tr>'
    ).join("") || '<tr><td colspan="6" class="empty">No features yet — create some via the board tools or API.</td></tr>';
  }
}

async function load(){
  try {
    const r = await api("/plugins/project_board/features");
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

setView(VIEW);   // sync the toggle + visibility to the initial view (list on mobile)
load();
setInterval(load, 10000);   // live-ish; the loop moves features under us
</script></body></html>"""
