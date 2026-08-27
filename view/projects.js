let kit;
let kitError = "";
try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
catch (error) { kitError = error instanceof Error ? error.message : String(error); }

const API = "/api/plugins/project_board/projects";
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>\"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;"}[c]));
const errorText = (error) => error instanceof Error ? error.message : String(error || "Unknown error");
const emptyState = () => ({projects:[], default_project:"", onboarding:{enabled:false,root:""}});
let state = emptyState();
let lastTrigger = null;
let loading = false;
let mutating = false;

async function request(path, init){
  const response = await kit.apiFetch(path, init);
  const data = await response.json().catch(() => { throw new Error(`HTTP ${response.status} (non-JSON response)`); });
  if (!response.ok){
    const detail = Array.isArray(data.detail)
      ? data.detail.map((item) => item?.msg || String(item)).join(" · ")
      : data.detail;
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return data;
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
function message(text, error=false){
  const el = error ? $("error") : $("notice");
  $(error ? "error-text" : "notice-text").textContent = text;
  el.hidden = !text;
  if (text) el.scrollIntoView({block:"nearest"});
}
function clearMessages(){ $("notice").hidden = true; $("error").hidden = true; }
function syncControls(){
  const busy = loading || mutating;
  $("app").setAttribute("aria-busy", busy ? "true" : "false");
  $("add").disabled = busy || !state.onboarding.enabled;
  $("retry").disabled = busy;
  $("editor-fields").disabled = mutating;
  $("cancel").disabled = mutating;
  $("projects").querySelectorAll("button").forEach((button) => {
    const editBlocked = button.hasAttribute("data-edit") && (!state.onboarding.enabled || button.dataset.editable === "false");
    button.disabled = busy || editBlocked;
  });
}

function render(){
  $("loading").hidden = true;
  const onboard = $("onboarding");
  onboard.hidden = state.onboarding.enabled;
  const root = state.onboarding.root ? ` Current root: ${state.onboarding.root}.` : " No onboarding root is configured.";
  onboard.textContent = state.onboarding.enabled ? "" : `Adding or editing projects is unavailable until Project onboarding is enabled and its root is set.${root} Existing projects remain visible; deletion remains available for unused entries.`;
  if (!state.projects.length){
    $("projects").innerHTML = '<div class="empty">No explicit board projects yet. Add one to replace the legacy single-repo configuration.</div>';
    syncControls();
    return;
  }
  $("projects").innerHTML = state.projects.map((project) => {
    const isDefault = project.name === state.default_project;
    const editable = project.editable !== false;
    const extras = project.extra_fields?.length ? `<div class="pl-callout">Preserved file-only fields: ${esc(project.extra_fields.join(", "))}</div>` : "";
    const malformed = editable ? "" : '<div class="pl-callout pl-callout--warning">This entry is not a mapping. Repair it in YAML or delete it; the editor will not overwrite it.</div>';
    return `<article class="panel project" data-name="${esc(project.name)}"><div class="row"><h2 tabindex="-1">${esc(project.name)}</h2>${isDefault?'<span class="badge">default</span>':""}</div>
      <dl class="meta"><dt>Repository</dt><dd>${esc(project.repo || "Not configured")}</dd><dt>Base branch</dt><dd>${esc(project.base_branch || "main")}</dd><dt>Local gate</dt><dd>${esc(project.local_gate_cmd || "Inherited / none")}</dd></dl>${extras}${malformed}
      <div class="actions"><button type="button" class="btn" data-edit="${esc(project.name)}" data-editable="${editable}" aria-label="Edit ${esc(project.name)}">Edit</button><button type="button" class="btn danger" data-delete="${esc(project.name)}" aria-label="Delete ${esc(project.name)}">Delete</button></div></article>`;
  }).join("");
  syncControls();
}

async function load({clear=true}={}){
  if (clear) clearMessages();
  loading = true; syncControls();
  try {
    state = normalized(await request(API, {cache:"no-store"}));
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
    $("editor").hidden = false;
    (editing ? $("repo") : $("name")).focus();
  }
}
function closeEditor(restoreFocus=true, force=false){
  if (mutating && !force) return;
  $("editor").hidden = true; $("form").reset(); $("name").disabled = false;
  if (restoreFocus && lastTrigger?.isConnected) lastTrigger.focus();
}
function focusProject(name){
  const target = [...$("projects").querySelectorAll("[data-edit]")].find((button) => button.dataset.edit === name);
  const heading = target?.closest(".project")?.querySelector("h2");
  const fallback = [...$("projects").querySelectorAll("button")].find((button) => !button.disabled)
    || (!$("add").disabled ? $("add") : $("page-title"));
  (target && !target.disabled ? target : heading || fallback).focus();
}

$("add").addEventListener("click", (event) => { lastTrigger = event.currentTarget; openEditor(null); });
$("cancel").addEventListener("click", () => closeEditor());
$("retry").addEventListener("click", () => {
  if (!kit){ location.reload(); return; }
  $("loading").hidden = false; $("loading").textContent = "Loading projects…"; load();
});
$("editor").addEventListener("keydown", (event) => { if (event.key === "Escape" && !mutating) closeEditor(); });
$("projects").addEventListener("click", async (event) => {
  const button = event.target.closest("button"); if (!button || mutating || loading) return;
  const edit = button.getAttribute("data-edit");
  if (edit){ lastTrigger = button; openEditor(state.projects.find((project) => project.name === edit)); return; }
  const remove = button.getAttribute("data-delete"); if (!remove) return;
  if (!window.confirm(`Delete board project “${remove}”? This is allowed only when no active board cards reference it.`)) return;
  mutating = true; button.textContent = "Deleting…"; button.closest(".project").setAttribute("aria-busy", "true"); clearMessages(); syncControls();
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
  } catch (error) { message(errorText(error), true); }
  finally {
    mutating = false;
    if (applied) render();
    else { button.textContent = "Delete"; button.closest(".project")?.removeAttribute("aria-busy"); syncControls(); }
    if (applied) focusProject("");
  }
});
$("form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (mutating || loading) return; clearMessages();
  const original = $("original-name").value;
  const name = original || $("name").value.trim();
  const defaultAction = $("default").checked ? "set" : original === state.default_project ? "clear" : "keep";
  const body = {repo:$("repo").value.trim(), base_branch:$("base").value.trim(), local_gate_cmd:$("gate").value.trim(), repo_conventions:$("conventions").value.trim(), default_action:defaultAction};
  mutating = true; $("save").textContent = "Saving…"; $("form").setAttribute("aria-busy", "true"); syncControls();
  let applied = false;
  let refreshed = false;
  try {
    const result = await request(`${API}/${encodeURIComponent(name)}`, {method:"PUT", headers:{"content-type":"application/json"}, body:JSON.stringify(body)});
    applied = true;
    const row = {name, ...result.entry};
    state.projects = [...state.projects.filter((project) => project.name !== name), row]
      .sort((left, right) => left.name.localeCompare(right.name));
    state.default_project = result.default_project || "";
    closeEditor(false, true); render();
    refreshed = await load({clear:false});
    if (refreshed) message(`Saved ${name}. The running board is already using this project.`);
    else message(`Saved ${name}, but the project list could not be refreshed. The change applied; Retry to reload live state.`, true);
  } catch (error) { message(errorText(error), true); }
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
