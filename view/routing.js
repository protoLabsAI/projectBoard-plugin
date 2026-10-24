// Configure ▸ Coder routing (#578) — a sandboxed Configure-tab page like Projects. It holds no
// config data: every read and save goes through the operator-bearer-gated
// /api/plugins/project_board/routing* routes via the DS kit's authed fetch.
let kit;
try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
catch (e) { kit = { initPluginView(){}, apiFetch: (p, i) => fetch(BASE + p, i) }; }
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const api = async (p) => {
  const r = await kit.apiFetch(p);
  const d = await r.json().catch(() => { throw new Error("HTTP " + r.status + " (non-JSON response)"); });
  if (!r.ok) throw new Error(d.detail || "HTTP " + r.status);
  return d;
};

// One question first: which coders may build? Each coder is a card with its provider state,
// its subscription usage and an "Allowed to build" switch that saves at once through B3's
// guarded, audited PUT /routing (switching opus off IS "Codex only" — one click). All coders
// allowed = no routing.exclusive. Below it, the ladder — shown ONCE for every project that
// shares it, since projects almost always do — read from GET /routing/effective, which
// computes "next" with the loop's own sibling-preference helpers, so the view can't drift.
let ROUTING_BUSY = false;        // a save is in flight — freeze the switches
let ROUTING_MSG = null;          // {text, kind} shown inline after a save / load

// A delegate's state across every project (the endpoint reports it per project).
function routingDelegates(body){
  const out = new Map();
  (body.delegates || []).forEach(n => out.set(n, {name: n, down: false, headroom: "unknown"}));
  Object.values(body.projects || {}).forEach(p => (p.delegates || []).forEach(d => {
    const cur = out.get(d.name) || {name: d.name, down: false, headroom: "unknown"};
    out.set(d.name, {name: d.name, down: cur.down || !!d.provider_is_down,
      headroom: d.headroom !== "unknown" ? d.headroom : cur.headroom});
  }));
  return [...out.values()];
}
function usageMeter(headroom){
  if (headroom === "unknown" || headroom == null)
    return '<div class="r-usage r-none">Usage unknown</div>';
  const used = Math.max(0, Math.min(100, Math.round(100 - Number(headroom))));
  const tone = used >= 90 ? "error" : used >= 80 ? "warning" : "success";
  return '<div class="pl-progress r-usage"><div class="pl-progress__caption"><span>Usage</span>'
    + '<span class="pl-progress__value">'+used+'% used</span></div>'
    + '<div class="pl-progress__track"><div class="pl-progress__fill pl-progress__fill--'+tone+'" style="width:'+used+'%"></div></div></div>';
}
function coderCard(d, allowed, lastAllowed){
  const lock = allowed && lastAllowed;
  return '<div class="pl-card r-coder'+(allowed?"":" r-coder--off")+'">'
    + '<div class="r-coder-head"><span class="pl-dot '+(d.down?"pl-dot--error":"pl-dot--success")+'"></span>'
    + '<span class="r-coder-name">'+esc(d.name)+'</span>'
    + '<span class="r-coder-state">'+(d.down?"provider down":"live")+'</span></div>'
    + usageMeter(d.headroom)
    + '<label class="pl-switch'+(lock?" pl-switch--disabled":"")+'"'+(lock?' title="At least one coder has to build."':"")+'>'
    + '<input type="checkbox" class="pl-switch__input" data-del="'+esc(d.name)+'"'+(allowed?" checked":"")+(lock?" disabled":"")+'>'
    + '<span class="pl-switch__track"><span class="pl-switch__thumb"></span></span>'
    + '<span class="pl-switch__label">Allowed to build</span></label></div>';
}
function routingSummary(dels, exclusive){
  if (!exclusive.length) return "All coders can build. Each card starts on the next coder in its rung and climbs the ladder when it needs to.";
  return "Only "+exclusive.join(" and ")+" can build. Every card, fix round and escalation stays on "
    + (exclusive.length === 1 ? "it" : "them") + ".";
}
function routingCoders(body){
  const dels = routingDelegates(body);
  if (!dels.length) return '<section class="r-sec"><h2>Coders</h2><p class="r-none">No coders are configured in any ladder yet.</p></section>';
  const exclusive = body.board_exclusive || [];
  const allowed = new Set(exclusive.length ? exclusive : dels.map(d => d.name));
  const msg = ROUTING_MSG
    ? '<div class="pl-callout pl-callout--'+(ROUTING_MSG.kind==="error"?"error":ROUTING_MSG.kind==="ok"?"success":"info")+' r-msg">'+esc(ROUTING_MSG.text)+'</div>'
    : "";
  return '<section class="r-sec"><div class="r-head"><h2>Coders</h2><span class="r-live">Switches apply live, no Save needed.</span></div>'
    + '<p class="r-summary">'+esc(routingSummary(dels, exclusive))+'</p>'
    + '<div class="r-coders">'+dels.map(d => coderCard(d, allowed.has(d.name), allowed.size === 1)).join("")+'</div>'
    + msg
    + '<p class="r-note">Covers building, CI and review fixes, and escalation. The review gate and helpers (decompose, Max-Mode judge, task assignees) keep their own delegates.</p>'
    + '</section>';
}
// Projects that route the same way share one ladder block. usage_policy (#581) is config, like
// exclusive, so key on the RULES (not their live account %) — a project with its own rules splits off.
function ladderKey(p){
  return JSON.stringify([
    p.exclusive || [], p.escalation_on,
    (p.ladder || []).map(r => [r.tier, r.delegates]),
    (p.usage_policy || []).map(r => [r.account, r.keep_under, r.prefer, r.tiers || []]),
  ]);
}
// #581 B7b: one usage_policy rule's live state — active once its account is known and over the
// target %, else inactive (under target, or "usage unknown" — never an error, D4).
function ruleStatus(r){
  const used = r.account_used_pct;
  if (used === "unknown" || used == null) return {text: "usage unknown — inactive", off: true};
  const u = Math.round(Number(used));
  return Number(used) >= Number(r.keep_under)
    ? {text: u+"% used ≥ "+esc(r.keep_under)+"% — active", off: false}
    : {text: u+"% used < "+esc(r.keep_under)+"% — inactive", off: true};
}
// The active rules for a ladder group, read-only. The deciding rule per rung is the "rule N" tag
// below; here the operator sees every rule and whether its account is currently over its target.
function policyRules(p){
  const rules = p.usage_policy || [];
  if (!rules.length) return "";
  const row = (r, i) => {
    const st = ruleStatus(r);
    const tiers = (r.tiers || []).length ? r.tiers.join(", ") : "all tiers";
    return '<li class="r-rule'+(st.off ? " r-rule--off" : "")+'">'
      + '<span class="r-rule-tag">rule '+(i+1)+'</span>'
      + '<span class="r-rule-body">keep <b>'+esc(r.account)+'</b> under '+esc(r.keep_under)+'%'
      + ' → prefer <b>'+esc(r.prefer)+'</b> <span class="r-rule-tiers">('+esc(tiers)+')</span></span>'
      + '<span class="r-rule-state">'+esc(st.text)+'</span></li>';
  };
  return '<div class="r-rules"><div class="r-rules-head">Usage rules</div>'
    + '<ol class="r-rules-list">'+rules.map(row).join("")+'</ol></div>';
}
function ladderRows(p){
  return (p.ladder || []).map(r => {
    const pol = r.policy || {};
    const tag = pol.rule_index != null ? ' <span class="r-rule-tag r-rule-tag--pick">rule '+(pol.rule_index+1)+'</span>' : '';
    return '<div class="r-rung"><span class="r-tier">'+esc(r.tier)+'</span>'
      + '<span class="r-path">'+(r.delegates.length ? r.delegates.map(esc).join(' <span class="r-then">then</span> ') : '<span class="r-none">no coder</span>')+'</span>'
      + (r.starts_on ? '<span class="r-next" title="'+esc(r.reason)+'">next: '+esc(r.starts_on)+tag+'</span>' : '')
      + '</div>';
  }).join("");
}
function routingLadders(body){
  const projects = body.projects || {};
  const names = Object.keys(projects).sort();
  if (!names.length) return '<section class="r-sec"><h2>Ladder</h2><p class="r-none">No projects are configured.</p></section>';
  const groups = new Map();
  names.forEach(n => { const k = ladderKey(projects[n]); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(n); });
  const ordered = [...groups.values()].sort((a, b) => b.length - a.length);
  const block = (members, i) => {
    const p = projects[members[0]];
    const who = members.length === names.length ? "All "+names.length+" projects"
      : (i === 0 ? members.length+" projects" : members.join(", "));
    const list = members.length === names.length || i !== 0 ? "" : '<div class="r-members">'+members.map(esc).join(", ")+'</div>';
    return '<div class="pl-card r-ladder"><div class="r-ladder-head">'+esc(who)
      + (p.escalation_on ? "" : ' <span class="r-none">· no escalation</span>')+'</div>'+list
      + policyRules(p) + ladderRows(p) + '</div>';
  };
  return '<section class="r-sec"><h2>Ladder</h2>'
    + '<p class="r-note">Cards start on the standard rung and climb when a coder runs out of time or attempts. "Next" is the coder the next card would open on right now; a "rule N" tag means a usage rule steered it there.</p>'
    + ordered.map(block).join("") + '</section>';
}

async function renderRouting(){
  const host = $("routing");
  host.setAttribute("aria-busy", "true");
  // The effective read resolves every project's ladder and each coder's usage (a few
  // seconds): say so on first open instead of showing an empty tab.
  if (!host.firstChild) host.innerHTML = '<p class="r-loading">Reading coder routing…</p>';
  try {
    const body = await api("/api/plugins/project_board/routing/effective");
    host.innerHTML = routingCoders(body) + routingLadders(body);
  } catch (e) {
    host.innerHTML = '<div class="pl-callout pl-callout--error">Could not load routing: '+esc(String(e && e.message || e))+'</div>';
  } finally {
    host.removeAttribute("aria-busy");
    syncRoutingControls();
  }
}

function syncRoutingControls(){
  $("routing").querySelectorAll("input[data-del]").forEach(el => {
    if (ROUTING_BUSY) el.disabled = true;
  });
}

async function putRouting(exclusive){
  const r = await kit.apiFetch("/api/plugins/project_board/routing",
    {method:"PUT", headers:{"content-type":"application/json"}, body:JSON.stringify({exclusive})});
  // A host gate refusal (non-operator) is a bare 403; a validation refusal carries a detail.
  if (r.status === 403) throw new Error("Refused: changing routing needs an operator session. Nothing changed.");
  const d = await r.json().catch(() => { throw new Error("HTTP "+r.status+" (non-JSON response)"); });
  if (!r.ok) throw new Error(d.detail || ("HTTP "+r.status));
  return d;
}

async function routingSave(exclusive){
  if (ROUTING_BUSY) return;
  ROUTING_BUSY = true; ROUTING_MSG = {text:"Saving…", kind:null}; syncRoutingControls();
  try {
    await putRouting(exclusive);
    ROUTING_MSG = {text: exclusive.length
      ? ("Saved. Only "+exclusive.join(" and ")+" can build, starting with the next card.")
      : "Saved. All coders can build again.", kind:"ok"};
  } catch (e) {
    ROUTING_MSG = {text: String(e && e.message || e), kind:"error"};  // refusal surfaced inline
  } finally {
    ROUTING_BUSY = false;
    await renderRouting();   // re-read so the page shows exactly what landed
  }
}

// One delegated listener — the panel's innerHTML is rebuilt on every render. A switch saves
// at once; every coder allowed is the same as no exclusive, so it clears routing.exclusive.
$("routing").addEventListener("change", (e) => {
  const sw = e.target.closest("input[data-del]"); if (!sw || ROUTING_BUSY) return;
  const all = [...$("routing").querySelectorAll("input[data-del]")];
  const on = all.filter(i => i.checked).map(i => i.dataset.del);
  if (!on.length){ sw.checked = true; return; }
  routingSave(on.length === all.length ? [] : on);
});

let booted = false;
kit.initPluginView(() => { if (!booted){ booted = true; renderRouting(); } });
