let kit;
try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
catch (e) { kit = {initPluginView(){}, apiFetch:(p,i)=>fetch(BASE+p,i)}; }

const API = "/api/plugins/project_board/projects";
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>\"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;"}[c]));
let state = {projects:[], default_project:"", onboarding:{enabled:false,root:""}};
let lastTrigger = null;

async function request(path, init){
  const response = await kit.apiFetch(path, init);
  const data = await response.json().catch(() => { throw new Error(`HTTP ${response.status} (non-JSON response)`); });
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}
function message(text, error=false){
  const el = error ? $("error") : $("notice");
  $(error ? "error-text" : "notice-text").textContent = text; el.hidden = !text;
  if (text) el.scrollIntoView({block:"nearest"});
}
function clearMessages(){ $("notice").hidden = true; $("error").hidden = true; }

function render(){
  $("loading").hidden = true;
  const onboard = $("onboarding");
  onboard.hidden = state.onboarding.enabled;
  const root = state.onboarding.root ? ` Current root: ${state.onboarding.root}.` : " No onboarding root is configured.";
  onboard.textContent = state.onboarding.enabled ? "" : `Adding or editing projects is unavailable until Project onboarding is enabled and its root is set.${root} Existing projects remain visible; deletion still requires the operator API.`;
  $("add").disabled = !state.onboarding.enabled;
  if (!state.projects.length){
    $("projects").innerHTML = '<div class="empty">No explicit board projects yet. Add one to replace the legacy single-repo configuration.</div>';
    return;
  }
  $("projects").innerHTML = state.projects.map((p) => {
    const isDefault = p.name === state.default_project;
    const extras = p.extra_fields?.length ? `<div class="pl-callout">Preserved file-only fields: ${esc(p.extra_fields.join(", "))}</div>` : "";
    return `<article class="panel project" data-name="${esc(p.name)}"><div class="row"><h2>${esc(p.name)}</h2>${isDefault?'<span class="badge">default</span>':""}</div>
      <dl class="meta"><dt>Repository</dt><dd>${esc(p.repo)}</dd><dt>Base branch</dt><dd>${esc(p.base_branch || "main")}</dd><dt>Local gate</dt><dd>${esc(p.local_gate_cmd || "Inherited / none")}</dd></dl>${extras}
      <div class="actions"><button class="btn" data-edit="${esc(p.name)}" ${state.onboarding.enabled?"":"disabled"}>Edit</button><button class="btn danger" data-delete="${esc(p.name)}">Delete</button></div></article>`;
  }).join("");
}

async function load(){
  clearMessages();
  try { state = await request(API); render(); }
  catch (error) { $("loading").hidden = true; message(error.message, true); }
}
function openEditor(project){
  clearMessages();
  const editing = Boolean(project);
  $("editor-title").textContent = editing ? `Edit ${project.name}` : "Add project";
  $("original-name").value = editing ? project.name : "";
  $("name").value = editing ? project.name : ""; $("name").disabled = editing;
  $("repo").value = editing ? project.repo : "";
  $("base").value = editing ? (project.base_branch || "main") : "main";
  $("gate").value = editing ? (project.local_gate_cmd || "") : "";
  $("conventions").value = editing ? (project.repo_conventions || "") : "";
  $("default").checked = editing && project.name === state.default_project;
  $("editor").hidden = false; (editing ? $("repo") : $("name")).focus();
}
function closeEditor(restoreFocus=true){
  $("editor").hidden = true; $("form").reset(); $("name").disabled = false;
  if (restoreFocus && lastTrigger?.isConnected) lastTrigger.focus();
}

$("add").addEventListener("click", (event) => { lastTrigger = event.currentTarget; openEditor(null); });
$("cancel").addEventListener("click", closeEditor);
$("retry").addEventListener("click", () => { $("loading").hidden = false; $("loading").textContent = "Loading projects…"; load(); });
$("projects").addEventListener("click", async (event) => {
  const button = event.target.closest("button"); if (!button) return;
  const edit = button.getAttribute("data-edit");
  if (edit){ lastTrigger = button; openEditor(state.projects.find((p) => p.name === edit)); return; }
  const remove = button.getAttribute("data-delete"); if (!remove) return;
  if (!window.confirm(`Delete board project “${remove}”? This is allowed only when no board cards reference it.`)) return;
  button.disabled = true; button.textContent = "Deleting…"; button.closest(".project").setAttribute("aria-busy", "true"); clearMessages();
  try { await request(`${API}/${encodeURIComponent(remove)}`, {method:"DELETE"}); closeEditor(false); await load(); $("add").focus(); message(`Deleted ${remove}. The running board is already using the new registry.`); }
  catch (error) { button.disabled = false; button.textContent = "Delete"; button.closest(".project").removeAttribute("aria-busy"); message(error.message, true); }
});
$("form").addEventListener("submit", async (event) => {
  event.preventDefault(); clearMessages();
  const name = $("original-name").value || $("name").value.trim();
  const body = {repo:$("repo").value.trim(), base_branch:$("base").value.trim(), local_gate_cmd:$("gate").value.trim(), repo_conventions:$("conventions").value.trim(), make_default:$("default").checked};
  $("save").disabled = true; $("save").textContent = "Saving…"; $("form").setAttribute("aria-busy", "true");
  try { await request(`${API}/${encodeURIComponent(name)}`, {method:"PUT", headers:{"content-type":"application/json"}, body:JSON.stringify(body)}); closeEditor(false); await load(); $("add").focus(); message(`Saved ${name}. The running board is already using this project.`); }
  catch (error) { message(error.message, true); }
  finally { $("save").disabled = false; $("save").textContent = "Save project"; $("form").removeAttribute("aria-busy"); }
});

let booted=false; function boot(){if(booted)return;booted=true;load();}
kit.initPluginView(boot); setTimeout(boot,800);
