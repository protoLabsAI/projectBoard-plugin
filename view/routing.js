// Configure ▸ Coders (#578, #613) — a sandboxed Configure-tab page like Projects. It holds no
// config data: the coder allow/deny switches and ladder read/save through the operator-bearer-
// gated /api/plugins/project_board/routing* routes, and the scalar coder/capacity/advanced knobs
// read/save through the guarded /settings route (S0) — all via the DS kit's authed fetch.
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
// allowed = no routing.exclusive. The chosen DEFAULT coder (the `coder` setting) is a picker of
// the configured delegates, marked on its card; the capacity steppers and the Advanced block
// save through the guarded PUT /settings. Below it, the ladder — shown ONCE for every project
// that shares it, since projects almost always do — read from GET /routing/effective, which
// computes "next" with the loop's own sibling-preference helpers, so the view can't drift.
let ROUTING_BUSY = false;        // a routing (allow/deny) save is in flight — freeze the switches
let ROUTING_MSG = null;          // {text, kind} shown inline after a routing save / load
let SAVE_BUSY = false;           // a settings save is in flight — freeze the knobs
let SETTINGS_MSG = {};           // {key: {kind, text}} inline message per settings knob
let ROUTING_BODY = null;         // last /routing/effective read (coders + ladder)
let SETTINGS_ERROR = null;      // settings reads must not hide the independent routing controls
let SETTINGS = null;             // last /settings snapshot (scalar knobs + pending_restart)
let CODER_CUSTOM = false;        // the default-coder control is in free-text mode (type any delegate)

const settingMeta = (key) => ((SETTINGS && SETTINGS.settings) || {})[key] || null;
const settingValue = (key) => { const m = settingMeta(key); return m ? m.value : undefined; };

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
function coderCard(d, allowed, lastAllowed, isDefault){
  const lock = allowed && lastAllowed;
  return '<div class="pl-card r-coder'+(allowed?"":" r-coder--off")+(isDefault?" r-coder--default":"")+'">'
    + '<div class="r-coder-head"><span class="pl-dot '+(d.down?"pl-dot--error":"pl-dot--success")+'"></span>'
    + '<span class="r-coder-name">'+esc(d.name)+'</span>'
    + (isDefault ? '<span class="r-coder-default">default</span>' : '')
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

// ── The default-coder picker: the `coder` setting (#613). The routing read only knows the
// delegates named in a ladder (plus the current coder), so a picker of just those would strand a
// fresh board — no ladder means an empty list and no way to set the required coder — and would
// hide any configured delegate that no ladder names. So the control keeps the picker for the
// common case AND always offers free text: the delegates become a <select> with a trailing
// "Type a coder name…" option, and when that's chosen (or there's nothing to pick, or the saved
// coder isn't in any ladder) a text input takes any delegate the host provides. The host roster
// — not this list — is the authority on whether a name resolves, so a typed name is saved and
// setup checks confirm it; only an empty name is refused here (the loop needs one to build).
function coderPicker(){
  if (!SETTINGS) return "";
  const dels = (ROUTING_BODY && ROUTING_BODY.delegates) || [];
  const cur = String(settingValue("coder") || "");
  const known = dels.includes(cur);
  const custom = CODER_CUSTOM || !dels.length || (!!cur && !known);
  const msg = SETTINGS_MSG.coder;
  const opts = [];
  if (!cur) opts.push('<option value="" disabled hidden'+(custom?"":" selected")+'>Choose a coder…</option>');
  dels.forEach(d => opts.push('<option value="'+esc(d)+'"'+(d === cur && !custom ? " selected" : "")+'>'+esc(d)+'</option>'));
  opts.push('<option value="__custom__"'+(custom?" selected":"")+'>Type a coder name…</option>');
  const labelFor = dels.length ? "coder-picker" : "coder-text";
  let control = "";
  if (dels.length)
    control += '<select class="r-select" id="coder-picker"'+(SAVE_BUSY?" disabled":"")+'>'+opts.join("")+'</select>';
  if (custom)
    control += '<input type="text" class="r-text r-coder-text" id="coder-text" data-coder-text'
      + ' value="'+esc(cur)+'" placeholder="delegate name" spellcheck="false" autocomplete="off"'
      + (SAVE_BUSY?" disabled":"")+'>';
  return '<div class="r-knob r-picker">'
    + '<div class="r-knob-head"><label class="r-knob-label" for="'+labelFor+'">Default coder</label></div>'
    + control
    + '<p class="r-knob-help">Choose the configured coder that builds cards, or type the name of any delegate the host provides; changes take effect on the next check.</p>'
    + (dels.length ? "" : '<p class="r-none">No coders are configured in any ladder yet — type the delegate name that should build.</p>')
    + inlineMsg(msg)
    + '</div>';
}

function coderSection(){
  const body = ROUTING_BODY;
  const dels = routingDelegates(body);
  const def = String(settingValue("coder") || "");
  const head = '<div class="r-head"><h2>Coders</h2><span class="r-live">Switches apply live, no Save needed.</span></div>';
  if (!dels.length)
    return '<section class="r-sec">'+head+coderPicker()+'</section>';
  const exclusive = body.board_exclusive || [];
  const allowed = new Set(exclusive.length ? exclusive : dels.map(d => d.name));
  const msg = ROUTING_MSG
    ? '<div class="pl-callout pl-callout--'+(ROUTING_MSG.kind==="error"?"error":ROUTING_MSG.kind==="ok"?"success":"info")+' r-msg">'+esc(ROUTING_MSG.text)+'</div>'
    : "";
  return '<section class="r-sec">'+head
    + coderPicker()
    + '<p class="r-summary">'+esc(routingSummary(dels, exclusive))+'</p>'
    + '<div class="r-coders">'+dels.map(d => coderCard(d, allowed.has(d.name), allowed.size === 1, d.name === def)).join("")+'</div>'
    + msg
    + '<p class="r-note">Covers building, CI and review fixes, and escalation. The review gate and helpers (decompose, Max-Mode judge, task assignees) keep their own delegates.</p>'
    + '</section>';
}

// ── Capacity: two LIVE steppers with a one-line consequence that updates as you step. ─────────
const CONSEQUENCE = {
  max_concurrent: (n) => n <= 1 ? "One card builds at a time." : "Up to " + n + " cards build at once.",
  max_pending_reviews: (n) => n === 0
    ? "No limit — new builds never pause for the review queue."
    : "Pause new builds once " + n + " pull request" + (n === 1 ? " waits" : "s wait") + " for review.",
};
function stepBtn(key, dir, disabled, label){
  return '<button type="button" class="r-step" data-step="'+esc(key)+'" data-dir="'+dir+'"'
    + (disabled?" disabled":"")+' aria-label="'+esc(label)+'">'+(dir<0?"−":"+")+'</button>';
}
function numberStepper(key, label, help, consequence){
  const meta = settingMeta(key) || {};
  const value = Number(meta.value);
  const min = meta.minimum, max = meta.maximum;
  return '<div class="r-knob">'
    + '<div class="r-knob-head"><span class="r-knob-label">'+esc(label)+'</span></div>'
    + '<div class="r-stepper" role="group" aria-label="'+esc(label)+'">'
    + stepBtn(key, -1, SAVE_BUSY || !(value > min), "Decrease " + label)
    + '<span class="r-step-val" data-val="'+esc(key)+'">'+value+'</span>'
    + stepBtn(key, 1, SAVE_BUSY || !(value < max), "Increase " + label)
    + '</div>'
    + '<p class="r-knob-help">'+esc(help)+'</p>'
    + (consequence ? '<p class="r-consequence" data-consequence="'+esc(key)+'">'+esc(consequence(value))+'</p>' : "")
    + inlineMsg(SETTINGS_MSG[key])
    + '</div>';
}
function capacitySection(){
  return '<section class="r-sec"><div class="r-head"><h2>Capacity</h2>'
    + '<span class="r-live">Steppers apply live, no restart.</span></div>'
    + '<div class="r-knobs">'
    + numberStepper("max_concurrent", "Parallel builds",
        "Limit how many cards build at once; lowering this limit lets running builds finish.", CONSEQUENCE.max_concurrent)
    + numberStepper("max_pending_reviews", "Review-queue limit",
        "Pause new builds when this many pull requests await review; zero removes the limit.", CONSEQUENCE.max_pending_reviews)
    + '</div></section>';
}

// ── Advanced (collapsed): the tuning knobs most boards leave alone. Plain label + one sentence
// of help each (S6 copy). The restart-only ones nudge the host on save and ride the banner. ─────
const ADVANCED = [
  {key:"max_concurrent_sessions", label:"Parallel coding sessions per build", kind:"number",
   help:"Limit simultaneous coding sessions within each build; zero allows as many as the candidate budget permits."},
  {key:"coder_solve", label:"Try solutions until tests pass", kind:"bool",
   help:"Try and test alternative solutions when the coding plugin, acceptance criteria, and a test command are available."},
  {key:"coder_solve_test_cmd", label:"Tests for candidate solutions", kind:"text", mono:true,
   help:"Run this command to test candidate solutions, or leave blank to use the checks configured before opening a pull request."},
  {key:"max_mode_n", label:"Alternative builds to compare", kind:"number",
   help:"Build each fresh card this many ways and keep the best result; one turns off alternatives, while higher values multiply coding cost."},
  {key:"decompose_after_timeouts", label:"Timeouts before asking to split a card", kind:"number",
   help:"After this many fresh-build timeouts, pause the card and ask to split it into smaller tasks; zero disables the request."},
  {key:"goal_verify", label:"Check acceptance criteria before review", kind:"bool",
   help:"Have an independent model check the changes against the card’s acceptance criteria, including test coverage, before opening a pull request."},
];
function boolKnob(spec){
  const on = (settingMeta(spec.key) || {}).value === true;
  return '<div class="r-knob">'
    + '<label class="pl-switch'+(SAVE_BUSY?" pl-switch--disabled":"")+'">'
    + '<input type="checkbox" class="pl-switch__input" data-bool="'+esc(spec.key)+'"'+(on?" checked":"")+(SAVE_BUSY?" disabled":"")+'>'
    + '<span class="pl-switch__track"><span class="pl-switch__thumb"></span></span>'
    + '<span class="pl-switch__label">'+esc(spec.label)+'</span></label>'
    + '<p class="r-knob-help">'+esc(spec.help)+'</p>'
    + inlineMsg(SETTINGS_MSG[spec.key])
    + '</div>';
}
function textKnob(spec){
  const value = String((settingMeta(spec.key) || {}).value ?? "");
  return '<div class="r-knob">'
    + '<div class="r-knob-head"><label class="r-knob-label" for="knob-'+esc(spec.key)+'">'+esc(spec.label)+'</label></div>'
    + '<input type="text" id="knob-'+esc(spec.key)+'" class="r-text'+(spec.mono?" r-mono":"")+'" data-text="'+esc(spec.key)+'"'
    + ' value="'+esc(value)+'" spellcheck="false" autocomplete="off"'+(SAVE_BUSY?" disabled":"")+'>'
    + '<p class="r-knob-help">'+esc(spec.help)+'</p>'
    + inlineMsg(SETTINGS_MSG[spec.key])
    + '</div>';
}
function advancedKnob(spec){
  if (spec.kind === "number") return numberStepper(spec.key, spec.label, spec.help, null);
  if (spec.kind === "bool") return boolKnob(spec);
  return textKnob(spec);
}
function advancedSection(){
  return '<details class="r-sec r-advanced">'
    + '<summary class="r-adv-summary"><span class="r-adv-title">Advanced</span>'
    + '<span class="r-adv-note">Tuning for solve, Max-Mode and card splitting — most boards leave these alone.</span></summary>'
    + '<div class="r-knobs r-adv-knobs">' + ADVANCED.map(advancedKnob).join("") + '</div></details>';
}

// ── Pending restart: the saved, restart-only knobs not yet live, listed by LABEL (never key). ──
function restartBanner(settings){
  const pending = (settings && settings.pending_restart) || [];
  if (!pending.length) return "";
  const labels = pending.map(p => esc(p.label || p.key)).join(", ");
  const n = pending.length;
  const noun = n === 1 ? "change takes" : "changes take";
  return '<div class="pl-callout pl-callout--info r-restart">'
    + '<div class="r-restart-title">' + n + " " + noun + ' effect after a restart</div>'
    + '<p class="r-restart-list">' + labels + '</p>'
    + '<p class="r-restart-how">Restart this board member — stop and start it (or restart the agent) — to apply '
    + (n === 1 ? "it" : "them") + '.</p></div>';
}

function inlineMsg(m){
  if (!m) return "";
  if (m.kind === "error") return '<div class="pl-callout pl-callout--error r-inline">'+esc(m.text)+'</div>';
  return '<p class="'+(m.kind === "ok" ? "r-saved" : "r-saving")+'">'+esc(m.text)+'</p>';
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

function renderAll(){
  const host = $("routing");
  if (!ROUTING_BODY) return;
  host.innerHTML = restartBanner(SETTINGS)
    + coderSection()
    + (SETTINGS_ERROR ? inlineMsg({kind:"error", text:"Could not load settings: " + SETTINGS_ERROR}) : "")
    + (SETTINGS ? capacitySection() + advancedSection() : "")
    + routingLadders(ROUTING_BODY);
  syncRoutingControls();
}

async function renderRouting(){
  const host = $("routing");
  host.setAttribute("aria-busy", "true");
  // The effective read resolves every project's ladder and each coder's usage (a few
  // seconds): say so on first open instead of showing an empty tab.
  if (!host.firstChild) host.innerHTML = '<p class="r-loading">Reading coders and routing…</p>';
  SETTINGS_ERROR = null;
  try {
    const [body, settings] = await Promise.all([
      api("/api/plugins/project_board/routing/effective"),
      api("/api/plugins/project_board/settings").catch(e => {
        SETTINGS_ERROR = String(e && e.message || e);
        return null; // Hide unavailable scalar controls, but keep routing usable.
      }),
    ]);
    ROUTING_BODY = body; SETTINGS = settings;
    renderAll();
  } catch (e) {
    host.innerHTML = '<div class="pl-callout pl-callout--error">Could not load coders: '+esc(String(e && e.message || e))+'</div>';
  } finally {
    host.removeAttribute("aria-busy");
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

// Scalar knobs (coder / capacity / advanced) save through the guarded PUT /settings (S0).
async function putSettings(updates){
  const r = await kit.apiFetch("/api/plugins/project_board/settings",
    {method:"PUT", headers:{"content-type":"application/json"}, body:JSON.stringify(updates)});
  if (r.status === 403) throw new Error("Refused: changing settings needs an operator session. Nothing changed.");
  const d = await r.json().catch(() => { throw new Error("HTTP "+r.status+" (non-JSON response)"); });
  if (!r.ok) throw new Error(d.detail || ("HTTP "+r.status));
  return d;
}

// A restart-only knob just saved: nudge the host to show its own pending-restart affordance.
// Harmless before core #4489 ships (no listener answers), and our banner already lists it.
function postRestartPending(result){
  try {
    window.parent.postMessage({
      type: "protoagent:settings:restart-pending",
      plugin: "project_board",
      keys: ((result && result.pending_restart) || []).map(p => p.key),
    }, "*");
  } catch (e) { /* no host frame */ }
}

async function saveSetting(key, value){
  if (SAVE_BUSY) return;
  const restart = !!(settingMeta(key) || {}).restart;
  SAVE_BUSY = true;
  SETTINGS_MSG = {[key]: {kind:null, text:"Saving…"}};
  // Optimistic: reflect the submitted value at once so a stepper's consequence line updates
  // immediately; the re-read below replaces it with exactly what the host saved.
  if (SETTINGS && SETTINGS.settings && SETTINGS.settings[key]) SETTINGS.settings[key].value = value;
  renderAll();
  try {
    const result = await putSettings({[key]: value});
    SETTINGS_MSG = {[key]: {kind:"ok", text: restart ? "Saved — restart to apply." : "Saved."}};
    if (restart) postRestartPending(result);
  } catch (e) {
    SETTINGS_MSG = {[key]: {kind:"error", text: String(e && e.message || e)}};
  } finally {
    SAVE_BUSY = false;
    try { SETTINGS = await api("/api/plugins/project_board/settings"); } catch (_e) { /* keep last snapshot */ }
    renderAll();
  }
}

// Save the chosen coder. A typed name may be any delegate the host provides — a fresh board or
// one whose coder isn't in any ladder could never set it otherwise — so a typed name is accepted
// and setup checks confirm it resolves; an empty one is refused inline (the loop needs a coder to
// build). A pick that comes straight off the <select> can only ever be a configured delegate, but
// guard it anyway: an unconfigured value there (a stale option, or a future bug) is refused inline
// and never submitted. `typed` marks the free-text path.
function chooseCoder(name, typed){
  name = String(name || "").trim();
  if (typed){
    if (!name){
      SETTINGS_MSG = {coder: {kind:"error", text: "Enter a coder name — the loop needs one to build."}};
      renderAll();
      return;
    }
    saveSetting("coder", name);
    return;
  }
  const valid = new Set((ROUTING_BODY && ROUTING_BODY.delegates) || []);
  if (name && !valid.has(name)){
    SETTINGS_MSG = {coder: {kind:"error", text: "“"+name+"” isn’t a configured coder. Pick one from the list, or choose “Type a coder name…”."}};
    renderAll();
    return;
  }
  saveSetting("coder", name);
}
function stepKnob(key, dir){
  const meta = settingMeta(key) || {};
  const next = Math.max(meta.minimum, Math.min(meta.maximum, Number(meta.value) + dir));
  if (next === Number(meta.value)) return;
  saveSetting(key, next);
}

// Delegated listeners — the panel's innerHTML is rebuilt on every render.
$("routing").addEventListener("change", (e) => {
  // The allow/deny switch saves at once through PUT /routing; every coder allowed clears the
  // exclusive. Unchanged from #578.
  const sw = e.target.closest("input[data-del]");
  if (sw){
    if (ROUTING_BUSY) return;
    const all = [...$("routing").querySelectorAll("input[data-del]")];
    const on = all.filter(i => i.checked).map(i => i.dataset.del);
    if (!on.length){ sw.checked = true; return; }
    routingSave(on.length === all.length ? [] : on);
    return;
  }
  if (SAVE_BUSY) return;
  const picker = e.target.closest("#coder-picker");
  if (picker){
    if (picker.value === "__custom__"){
      CODER_CUSTOM = true; SETTINGS_MSG = {}; renderAll();
      const t = $("coder-text"); if (t) t.focus();
      return;
    }
    CODER_CUSTOM = false; chooseCoder(picker.value); return;
  }
  const coderText = e.target.closest("[data-coder-text]");
  if (coderText){ chooseCoder(coderText.value, true); return; }
  const bool = e.target.closest("input[data-bool]");
  if (bool){ saveSetting(bool.dataset.bool, bool.checked); return; }
  const text = e.target.closest("input[data-text]");
  if (text){ saveSetting(text.dataset.text, text.value); return; }
});
$("routing").addEventListener("click", (e) => {
  const step = e.target.closest("[data-step]");
  if (!step || SAVE_BUSY) return;
  stepKnob(step.dataset.step, Number(step.dataset.dir));
});

let booted = false;
kit.initPluginView(() => { if (!booted){ booted = true; renderRouting(); } });
