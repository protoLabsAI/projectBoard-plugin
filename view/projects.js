let kit;
let kitError = "";
try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
catch (error) { kitError = error instanceof Error ? error.message : String(error); }

const API = "/api/plugins/project_board/projects";
// The checks dot reads the loop's live preflight from the board status route (both reads go
// through the authenticated kit fetch). A held project is red with its one-line reason.
const STATUS = "/api/plugins/project_board/status";
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>\"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;"}[c]));
const errorText = (error) => error instanceof Error ? error.message : String(error || "Unknown error");
const emptyState = () => ({projects:[], default_project:"", onboarding:{enabled:false,root:""}});
let state = emptyState();
let checks = {held:{}, failures:{}, evaluated:false};
let lastTrigger = null;
let loading = false;
let mutating = false;

async function request(path, init){
  const response = await kit.apiFetch(path, init);
  const data = await response.json().catch(() => null);
  if (!response.ok || data === null){
    const detail = data && (Array.isArray(data.detail)
      ? data.detail.map((item) => item?.msg || String(item)).join(" · ")
      : data.detail);
    const error = new Error(detail || `HTTP ${response.status}${data === null ? " (non-JSON response)" : ""}`);
    error.status = response.status;  // lets a save tell "refused" from "the proxy gave up"
    throw error;
  }
  return data;
}

// A save that sets a new gate command, or moves the project to another repo, answers only
// after that gate has run once on the clean base: minutes, for a full suite (#393). The
// fleet proxy gives a plugin API call 20s and then answers 504, although the member keeps
// running the gate. So a 502/504 or a dropped connection is not a failure here. The save's
// outcome is read back from GET /projects (`saves.<name>`, matched by our request id).
const SAVE_POLL_MS = 3000;
const SAVE_WAIT_MS = 20 * 60 * 1000;
const newRequestId = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`);
function gateWillRun(body, current){
  const gate = body.local_gate_cmd;
  if (!gate || gate === "auto") return false;
  return !current || gate !== (current.local_gate_cmd || "") || body.repo !== (current.repo || "");
}
const proxyGaveUp = (error) => !error.status || error.status === 502 || error.status === 504;
async function awaitSaveOutcome(name, requestId){
  const deadline = Date.now() + SAVE_WAIT_MS;
  let unseen = 0;
  editorMessage(`The connection gave up before ${name}'s gate finished, but the agent is still running it. Waiting for the result…`);
  while (Date.now() < deadline){
    await new Promise((resolve) => setTimeout(resolve, SAVE_POLL_MS));
    let data;
    try { data = await request(API, {cache:"no-store"}); } catch { continue; }
    const save = data.saves?.[name];
    if (!save || save.id !== requestId){
      if (save?.id && save.state !== "running") throw new Error(`Another save of ${name} replaced this one — reload to see what was saved.`);
      if (++unseen >= 5) throw new Error(`The save of ${name} never reached the agent — try again.`);
      continue;
    }
    if (save.state === "running") continue;
    if (save.state === "saved") return {entry: data.projects.find((project) => project.name === name) || {}, default_project: data.default_project};
    throw new Error(save.detail || `${name} was not saved.`);
  }
  throw new Error(`${name}'s gate is still running after ${SAVE_WAIT_MS / 60000} minutes — reload the list later to see whether it was saved.`);
}
function normalized(data){
  return {
    projects:Array.isArray(data?.projects) ? data.projects : [],
    default_project:typeof data?.default_project === "string" ? data.default_project : "",
    onboarding:{
      enabled:Boolean(data?.onboarding?.enabled),
      root:typeof data?.onboarding?.root === "string" ? data.onboarding.root : "",
    },
  };
}
// The loop publishes an EMPTY preflight before its first pass and when it's off, and only HELD
// projects keep a reason. So a non-empty held/dirty/slow/oracle map is the signal that a
// verdict exists at all: with one, projects not flagged read as passing; with none, every dot
// reads "not run" (no verdict yet / loop off) — never a false green.
function checksFrom(status){
  const pf = (status && status.preflight) || {};
  const held = pf.held || {};
  const evaluated = [held, pf.dirty, pf.slow, pf.unwinnable_oracle]
    .some((m) => m && typeof m === "object" && Object.keys(m).length > 0);
  return {held, failures: pf.failures || {}, evaluated};
}
function dotFor(name){
  if (checks.held && Object.prototype.hasOwnProperty.call(checks.held, name)){
    const failure = checks.failures?.[name];
    return {variant:"pl-dot--error", reason:(failure?.headline) || checks.held[name] || "Held behind a red gate.", title:""};
  }
  if (checks.evaluated) return {variant:"pl-dot--success", reason:"", title:"Checks pass."};
  return {variant:"", reason:"", title:"No preflight verdict yet."};
}
// Home dir shown as `~` so a row stays short (e.g. `~/dev/x`).
function shortRepo(repo){
  if (!repo) return "Not configured";
  return String(repo).replace(/^\/(?:Users|home)\/[^/]+/, "~").replace(/^\/root(?=\/|$)/, "~");
}
function message(text, error=false){
  const el = error ? $("error") : $("notice");
  $(error ? "error-text" : "notice-text").textContent = text;
  el.hidden = !text;
  if (text) el.scrollIntoView({block:"nearest"});
}
// Refusals and the gate-wait line show INSIDE the open side panel; a page-top callout would
// sit behind the panel. Save success is reported at the top because the panel is closed by then.
function editorMessage(text, error=false){
  const el = $("editor-msg");
  el.textContent = text;
  el.className = "pl-callout" + (error ? " pl-callout--error" : "");
  el.hidden = !text;
  if (text) el.scrollIntoView({block:"nearest"});
}
function clearMessages(){ $("notice").hidden = true; $("error").hidden = true; $("editor-msg").hidden = true; }
// Editing needs a consented SPACE, not just the switch. The host refuses any write whose
// repo doesn't resolve under `onboarding.root`, so gating on `enabled` alone would leave a
// live button whose every submit fails server-side — which is what defaulting `enabled` on
// (protoAgent#3396) would otherwise have produced here.
function canEdit(){ return state.onboarding.enabled && Boolean(state.onboarding.root); }

function syncControls(){
  const busy = loading || mutating;
  $("app").setAttribute("aria-busy", busy ? "true" : "false");
  $("add").disabled = busy || !canEdit();
  $("retry").disabled = busy;
  $("editor-fields").disabled = mutating;
  $("cancel").disabled = mutating;
  $("save").disabled = mutating || !canEdit();
  $("projects").querySelectorAll("button").forEach((button) => {
    if (button.hasAttribute("data-open")){
      // Opening the editor needs a usable onboarding space; a non-mapping entry is not editable.
      button.disabled = busy || !canEdit() || button.dataset.editable === "false";
    } else {
      // Delete stays available even when adding/editing is blocked — unused entries can go.
      button.disabled = busy;
    }
  });
}

function render(){
  $("loading").hidden = true;
  // Name the ONE thing that is missing, and where to set it — the old copy described both
  // bounds at once whichever was unmet, so it read as a wall rather than an instruction.
  const onboard = $("onboarding");
  onboard.hidden = canEdit();
  const tail = " Existing projects remain visible; deletion remains available for unused entries.";
  onboard.textContent = canEdit() ? "" : (state.onboarding.enabled
    ? `Adding or editing projects needs an onboarding root: set Settings ▸ Capabilities ▸ Project onboarding ▸ Onboarding root. Repos must resolve under it.${tail}`
    : `Adding or editing projects needs Project onboarding switched on: Settings ▸ Capabilities ▸ Project onboarding.${tail}`);
  if (!state.projects.length){
    $("projects").innerHTML = '<div class="empty">No explicit board projects yet. Add one to replace the legacy single-repo configuration.</div>';
    syncControls();
    return;
  }
  $("projects").innerHTML = state.projects.map((project) => {
    const isDefault = project.name === state.default_project;
    const editable = project.editable !== false;
    const dot = editable ? dotFor(project.name) : {variant:"", reason:"", title:"Not a mapping — repair in YAML."};
    const reason = dot.reason ? `<span class="prow-reason" title="${esc(dot.reason)}">${esc(dot.reason)}</span>` : "";
    const def = isDefault ? '<span class="badge">default</span>' : "";
    const note = editable ? "" : '<div class="pl-callout pl-callout--warning prow-note">This entry is not a mapping. Repair it in YAML or delete it; the editor will not overwrite it.</div>';
    const openTitle = editable ? (dot.reason || dot.title || "") : "This entry is not a mapping.";
    return `<div class="prow-wrap${editable ? "" : " malformed"}" data-name="${esc(project.name)}">
      <button type="button" class="prow" data-open="${esc(project.name)}" data-editable="${editable}" aria-label="Edit ${esc(project.name)}" title="${esc(openTitle)}">
        <span class="pl-dot ${dot.variant}"></span>
        <span class="prow-name">${esc(project.name)}</span>
        <span class="prow-repo">${esc(shortRepo(project.repo))}</span>
        <span class="prow-branch">${esc(project.base_branch || "main")}</span>
        <span class="prow-tail">${reason}${def}</span>
      </button>
      <button type="button" class="btn danger prow-del" data-delete="${esc(project.name)}" aria-label="Delete ${esc(project.name)}">Delete</button>
      ${note}</div>`;
  }).join("");
  syncControls();
}

async function load({clear=true}={}){
  if (clear) clearMessages();
  loading = true; syncControls();
  try {
    // The list is primary; the dots are best-effort. A status read that fails leaves every dot
    // "not run" rather than failing the whole page.
    const [projData, statusData] = await Promise.all([
      request(API, {cache:"no-store"}),
      request(STATUS, {cache:"no-store"}).catch(() => null),
    ]);
    state = normalized(projData);
    checks = checksFrom(statusData);
    render();
    return true;
  } catch (error) {
    $("loading").hidden = true;
    message(errorText(error), true);
    return false;
  } finally {
    loading = false; syncControls();
  }
}
function openEditor(project){
  if (!project || project.editable !== false){
    clearMessages();
    const editing = Boolean(project);
    $("editor-title").textContent = editing ? `Edit ${project.name}` : "Add project";
    $("original-name").value = editing ? project.name : "";
    $("name").value = editing ? project.name : ""; $("name").disabled = editing;
    $("repo").value = editing ? project.repo : "";
    $("base").value = editing ? (project.base_branch || "main") : "main";
    $("gate").value = editing ? (project.local_gate_cmd || "") : "";
    $("conventions").value = editing ? (project.repo_conventions || "") : "";
    $("default").checked = editing ? project.name === state.default_project : state.projects.length === 0;
    $("default").disabled = editing && state.projects.length === 1;
    $("checks").open = false;  // Checks (gate command + conventions) start collapsed.
    const extras = $("editor-extras");
    const fields = editing && project.extra_fields?.length ? project.extra_fields.join(", ") : "";
    extras.hidden = !fields;
    extras.textContent = fields ? `Preserved file-only fields: ${fields}` : "";
    $("editor").hidden = false;
    (editing ? $("repo") : $("name")).focus();
  }
}
function closeEditor(restoreFocus=true, force=false){
  if (mutating && !force) return;
  $("editor").hidden = true; $("form").reset(); $("name").disabled = false;
  $("editor-extras").hidden = true; $("editor-msg").hidden = true;
  if (restoreFocus && lastTrigger?.isConnected) lastTrigger.focus();
}
function focusProject(name){
  const target = [...$("projects").querySelectorAll("[data-open]")].find((button) => button.dataset.open === name);
  const fallback = [...$("projects").querySelectorAll("button")].find((button) => !button.disabled)
    || (!$("add").disabled ? $("add") : $("page-title"));
  (target && !target.disabled ? target : fallback).focus();
}

$("add").addEventListener("click", (event) => { lastTrigger = event.currentTarget; openEditor(null); });
$("cancel").addEventListener("click", () => closeEditor());
$("backdrop").addEventListener("click", () => closeEditor());
$("retry").addEventListener("click", () => {
  if (!kit){ location.reload(); return; }
  $("loading").hidden = false; $("loading").textContent = "Loading projects…"; load();
});
$("editor").addEventListener("keydown", (event) => { if (event.key === "Escape" && !mutating) closeEditor(); });
$("projects").addEventListener("click", async (event) => {
  const button = event.target.closest("button"); if (!button || mutating || loading) return;
  const open = button.getAttribute("data-open");
  if (open){ lastTrigger = button; openEditor(state.projects.find((project) => project.name === open)); return; }
  const remove = button.getAttribute("data-delete"); if (!remove) return;
  if (!window.confirm(`Delete board project “${remove}”? This is allowed only when no active board cards reference it.`)) return;
  mutating = true; button.textContent = "Deleting…"; button.closest(".prow-wrap").setAttribute("aria-busy", "true"); clearMessages(); syncControls();
  let applied = false;
  let refreshed = false;
  try {
    const result = await request(`${API}/${encodeURIComponent(remove)}`, {method:"DELETE"});
    applied = true;
    state.projects = state.projects.filter((project) => project.name !== remove);
    state.default_project = result.default_project || "";
    closeEditor(false, true); render();
    refreshed = await load({clear:false});
    if (refreshed) message(`Deleted ${remove}. The running board is already using the new registry.`);
    else message(`Deleted ${remove}, but the project list could not be refreshed. The change applied; Retry to reload live state.`, true);
  } catch (error) { message(errorText(error), true); }  // a refusal (409) shows inline, in plain words
  finally {
    mutating = false;
    if (applied) render();
    else { button.textContent = "Delete"; button.closest(".prow-wrap")?.removeAttribute("aria-busy"); syncControls(); }
    if (applied) focusProject("");
  }
});
$("form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (mutating || loading) return; clearMessages();
  const original = $("original-name").value;
  const name = original || $("name").value.trim();
  const defaultAction = $("default").checked ? "set" : original === state.default_project ? "clear" : "keep";
  const requestId = newRequestId();
  const body = {repo:$("repo").value.trim(), base_branch:$("base").value.trim(), local_gate_cmd:$("gate").value.trim(), repo_conventions:$("conventions").value.trim(), default_action:defaultAction, request_id:requestId};
  const runsGate = gateWillRun(body, original ? state.projects.find((project) => project.name === original) : null);
  mutating = true; $("save").textContent = runsGate ? "Running the gate…" : "Saving…"; $("form").setAttribute("aria-busy", "true"); syncControls();
  if (runsGate) editorMessage(`Saving ${name} runs its gate once on the clean base before anything is saved. For a full test suite that takes minutes; the result appears here.`);
  let applied = false;
  let refreshed = false;
  try {
    let result;
    try {
      result = await request(`${API}/${encodeURIComponent(name)}`, {method:"PUT", headers:{"content-type":"application/json"}, body:JSON.stringify(body)});
    } catch (error) {
      if (!proxyGaveUp(error)) throw error;
      result = await awaitSaveOutcome(name, requestId);
    }
    applied = true;
    const row = {name, ...result.entry};
    state.projects = [...state.projects.filter((project) => project.name !== name), row]
      .sort((left, right) => left.name.localeCompare(right.name));
    state.default_project = result.default_project || "";
    closeEditor(false, true); render();
    refreshed = await load({clear:false});
    if (refreshed) message(`Saved ${name}. The running board is already using this project.`);
    else message(`Saved ${name}, but the project list could not be refreshed. The change applied; Retry to reload live state.`, true);
  } catch (error) { editorMessage(errorText(error), true); }  // 409 (concurrent save) / red gate shown inline
  finally {
    mutating = false; $("save").textContent = "Save project"; $("form").removeAttribute("aria-busy");
    if (applied) render(); else syncControls();
    if (applied) focusProject(name);
  }
});

if (!kit){
  $("loading").hidden = true;
  message(`Could not load the authenticated plugin bridge. Reload this tab to retry.${kitError ? ` ${kitError}` : ""}`, true);
  syncControls();
} else {
  let booted = false;
  kit.initPluginView(() => { if (!booted){ booted = true; load(); } });
}
