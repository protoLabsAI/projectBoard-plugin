"""Review & merge Configure tab (S4, #614): the after-a-PR pipeline as four ordered step cards.

Host-free contracts in the shape of test_overview_view / test_coders_view: the page is served on
the PUBLIC router (not under /api), holds no board data, reads and writes only through the DS
plugin-kit's authed fetch after initPluginView, shows a loading line without a boot-fallback timer,
uses DS tokens with no hardcoded colours, and never prints a raw config key as visible copy. The
scalar knobs save through the guarded PUT /settings boundary (S0). A node harness exercises the
interactive bits (the four steps in order, the workflow/reviewer/merge-method pickers, the
dependent-cards radio, a step greying its attempts when off, and the inline save + restart nudge).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from project_board.review_view import REVIEW_PAGE

VIEW = Path(__file__).resolve().parent.parent / "view"

SETTINGS_API = "/api/plugins/project_board/settings"
ROUTING_API = "/api/plugins/project_board/routing/effective"

# The fifteen scalar knobs the Review & merge tab now owns, by raw key — used for the "no raw key
# as visible copy" sweep, never shown to an operator.
PAGE_OWNED = (
    "review_gate",
    "review_workflow",
    "reviewer",
    "review_dispatch",
    "review_fix_max",
    "ci_poll",
    "ci_rerun_max",
    "ci_fix_max",
    "auto_rebase",
    "rebase_fix_max",
    "auto_merge",
    "merge_method",
    "merged_verify_max",
    "merge_poll",
    "dep_gate",
)


def _style_block() -> str:
    return REVIEW_PAGE.split("<style>", 1)[1].split("</style>", 1)[0]


# ── the single source file assembles ─────────────────────────────────────────────


def test_review_page_is_one_self_contained_file():
    assert (VIEW / "review.html").is_file()
    # One file: the markup carries its own <style> and <script type="module"> inline.
    assert REVIEW_PAGE == (VIEW / "review.html").read_text(encoding="utf-8")
    assert "<style>" in REVIEW_PAGE and "</style>" in REVIEW_PAGE
    assert 'type="module"' in REVIEW_PAGE
    # No unresolved splice placeholder (this page is one file — nothing is spliced in).
    assert "_CSS__" not in REVIEW_PAGE and "_JS__" not in REVIEW_PAGE


# ── served on the PUBLIC router, 200, not under /api, no-store ────────────────────


def test_route_returns_200_and_is_not_under_api():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from project_board.api import build_router

    app = FastAPI()
    app.include_router(build_router({}), prefix="/plugins/project_board")
    client = TestClient(app)
    path = "/plugins/project_board/config/review"
    assert "/api" not in path  # the page chrome is public; the DATA lives on the /api prefix
    page = client.get(path)
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-store"
    assert page.text == REVIEW_PAGE


def test_the_data_router_does_not_serve_the_page():
    # The /api (gated) router owns DATA, never this page — the public router owns the chrome.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from project_board.api import build_data_router

    app = FastAPI()
    app.include_router(build_data_router({}), prefix="/api/plugins/project_board")
    assert TestClient(app).get("/api/plugins/project_board/config/review").status_code == 404


# ── the page holds NO board data: it all arrives through the authed API ───────────


def test_page_reads_through_the_authed_kit_after_init():
    assert 'import(BASE + "/_ds/plugin-kit.js")' in REVIEW_PAGE
    assert "kit.initPluginView(" in REVIEW_PAGE and "kit.apiFetch" in REVIEW_PAGE
    assert "/_ds/plugin-kit.css" in REVIEW_PAGE
    # Slug-aware base from the page's own path (never a hardcoded /api/…).
    assert 'location.pathname.split("/plugins/")[0]' in REVIEW_PAGE
    # The knobs from /settings; the reviewer delegate list from /routing/effective.
    assert SETTINGS_API in REVIEW_PAGE and ROUTING_API in REVIEW_PAGE
    # No server-rendered data island — the page is a static template.
    assert '<script type="application/json"' not in REVIEW_PAGE
    # Strict, like Overview: NO unauthenticated bare-fetch fallback that would race the bearer.
    assert "apiFetch: (p, i) => fetch" not in REVIEW_PAGE
    assert "Could not load the authenticated plugin bridge" in REVIEW_PAGE


def test_boot_is_initpluginview_only_with_no_settimeout_fallback():
    # The one boot path is initPluginView's callback — no timer that would load before the bearer.
    assert "setTimeout(boot" not in REVIEW_PAGE
    assert "setTimeout(load" not in REVIEW_PAGE
    assert "setTimeout(render" not in REVIEW_PAGE
    assert "if (!booted) { booted = true; load(); }" in REVIEW_PAGE
    # A loading line only once the reads are actually slow (~0.5s) — no flash on a fast board.
    assert "SLOW_MS = 500" in REVIEW_PAGE
    assert "Reading review & merge settings…" in REVIEW_PAGE


def test_all_fifteen_page_owned_keys_are_wired_into_the_page():
    # Every key the tab took over from the Review schema list is handled by the page (as a step
    # switch, stepper, picker or radio) — none silently stopped being configurable in the move.
    for key in PAGE_OWNED:
        assert key in REVIEW_PAGE, key


# ── every change saves through PUT /settings with an inline result + the restart nudge ─────────


def test_every_change_saves_through_put_settings_with_an_inline_result():
    assert 'method: "PUT"' in REVIEW_PAGE
    assert "JSON.stringify({ [key]: value })" in REVIEW_PAGE
    # LIVE knobs show a plain "Saved."; restart-only knobs say "restart to apply." instead.
    assert '"Saved."' in REVIEW_PAGE
    assert '"Saved — restart to apply."' in REVIEW_PAGE


def test_restart_only_knobs_nudge_the_host_and_ride_the_banner():
    assert "window.parent.postMessage(" in REVIEW_PAGE
    assert '"protoagent:settings:restart-pending"' in REVIEW_PAGE
    assert "pending_restart" in REVIEW_PAGE
    assert "effect after a restart" in REVIEW_PAGE
    assert "Restart this board member" in REVIEW_PAGE


# ── dependent cards: a labelled two-way radio with the trade-off ──────────────────


def test_dependent_cards_is_a_labelled_two_way_radio_with_the_trade_off():
    assert 'type="radio" name="dep-gate"' in REVIEW_PAGE
    assert 'data-dep-gate="' in REVIEW_PAGE  # the value is spliced at render (asserted live below)
    # The two values map to dep_gate merge/review — in the control, never as visible copy.
    assert 'value: "merge"' in REVIEW_PAGE and 'value: "review"' in REVIEW_PAGE
    # Plain labels with the trade-off spelled out.
    assert "When the blocker merges" in REVIEW_PAGE
    assert "When the blocker opens a pull request" in REVIEW_PAGE
    assert "safer" in REVIEW_PAGE and "faster" in REVIEW_PAGE


# ── DS tokens only; no hardcoded colours ──────────────────────────────────────────


def test_styles_use_ds_tokens_and_no_hardcoded_colours():
    style = _style_block()
    # No id selectors are used in the stylesheet, so any `#` would be a hex colour literal.
    assert "#" not in style
    assert "rgb(" not in style and "rgba(" not in style and "hsl(" not in style
    # Colours come from DS tokens (light + dark).
    assert "var(--pl-color-bg)" in style and "var(--pl-color-fg)" in style
    assert "var(--pl-color-accent)" in style


# ── the live behaviours: steps in order, pickers, radio, greying, save + restart nudge ─────────


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_live_pipeline_pickers_greying_and_save():
    """Run the real page script with API-shaped reads and a stubbed host frame."""
    script = REVIEW_PAGE.split('<script type="module">', 1)[1].split("</script>", 1)[0]
    script = script.replace('import(BASE + "/_ds/plugin-kit.js")', "globalThis.testKit")
    harness = r"""
import assert from 'node:assert/strict';
const BASE = '';
let markup = '';
const listeners = {};
const posted = [];
const host = {
  setAttribute() {}, removeAttribute() {},
  addEventListener(type, fn) { listeners[type] = fn; },
  get firstChild() { return markup ? {} : null; },
  set innerHTML(v) { markup = v; },
  get innerHTML() { return markup; },
};
globalThis.document = { getElementById(id) { return id === 'review' ? host : null; } };
globalThis.window = { parent: { postMessage(m) { posted.push(m); } } };

const ROUTING_DATA = { delegates: ['codex', 'proto'], board_exclusive: [], projects: {} };
const META = {
  review_gate: {type: 'bool', restart: true, value: true, label: 'Require review before merging'},
  review_workflow: {type: 'string', restart: true, value: 'code-review', label: 'Workflow for required reviews'},
  reviewer: {type: 'string', restart: true, value: 'codex', label: 'Agent for pull request reviews'},
  review_dispatch: {type: 'bool', restart: true, value: false, label: 'Request an extra review on opening'},
  review_fix_max: {type: 'number', minimum: 0, maximum: 20, restart: true, value: 2, label: 'Attempts to fix review findings'},
  ci_poll: {type: 'bool', restart: true, value: true, label: 'Send failed checks back to the coder'},
  ci_rerun_max: {type: 'number', minimum: 0, maximum: 5, restart: true, value: 1, label: 'Rerun failed checks before changing code'},
  ci_fix_max: {type: 'number', minimum: 0, maximum: 20, restart: true, value: 2, label: 'Attempts to fix failed checks'},
  auto_rebase: {type: 'bool', restart: true, value: true, label: 'Update pull requests with base changes'},
  rebase_fix_max: {type: 'number', minimum: 0, maximum: 20, restart: true, value: 1, label: 'Attempts to fix merge conflicts'},
  auto_merge: {type: 'bool', restart: false, value: true, label: 'Merge approved pull requests automatically'},
  merge_method: {type: 'select', options: ['squash', 'merge', 'rebase'], restart: true, value: 'squash', label: 'How to merge pull requests'},
  merged_verify_max: {type: 'number', minimum: 0, maximum: 20, restart: false, value: 5, label: 'Failed checks before holding a merge'},
  merge_poll: {type: 'bool', restart: true, value: true, label: 'Check pull request status automatically'},
  dep_gate: {type: 'select', options: ['merge', 'review'], restart: true, value: 'merge', label: 'When dependent cards can start'},
};
const BOOT = {};
for (const k of Object.keys(META)) BOOT[k] = META[k].value;
function settings() {
  const s = {};
  for (const [k, m] of Object.entries(META)) s[k] = {...m};
  const pending = Object.entries(META)
    .filter(([k, m]) => m.restart && m.value !== BOOT[k])
    .map(([k, m]) => ({key: k, label: m.label || k}));
  return {settings: s, pending_restart: pending};
}
const calls = [];
globalThis.testKit = {
  initPluginView(cb) { this._boot = cb; },
  async apiFetch(path, init) {
    const method = (init && init.method) || 'GET';
    const body = init && init.body ? JSON.parse(init.body) : null;
    calls.push({path, method, body});
    if (method === 'PUT' && path.endsWith('/settings')) {
      for (const [k, v] of Object.entries(body)) if (META[k]) META[k].value = v;
      const snap = settings();
      return {ok: true, status: 200, async json() { return {ok: true, saved: Object.keys(body), pending_restart: snap.pending_restart}; }};
    }
    if (path.endsWith('/routing/effective')) return {ok: true, status: 200, async json() { return ROUTING_DATA; }};
    if (path.endsWith('/settings')) return {ok: true, status: 200, async json() { return settings(); }};
    return {ok: true, status: 200, async json() { return {}; }};
  },
};
function el(matches, props) { return Object.assign({closest(s) { return matches.includes(s) ? this : null; }}, props); }
const flush = async () => { for (let i = 0; i < 14; i++) await new Promise(r => setImmediate(r)); };
const strip = (s) => s.replace(/<[^>]*>/g, ' ');
const PAGE_OWNED = PAGE_OWNED_JSON;
"""
    harness = harness.replace("PAGE_OWNED_JSON", json.dumps(list(PAGE_OWNED)))
    assertions = r"""
globalThis.testKit._boot();
await flush();

// The four steps render in pipeline order: Review → CI → Rebase → Merge.
const order = ['Review', 'CI', 'Rebase', 'Merge'].map(t => markup.indexOf('>' + t + '</h2>'));
assert.ok(order.every((v, i) => v >= 0 && (i === 0 || v > order[i - 1])), 'four steps in order');
for (const n of ['1', '2', '3', '4'])
  assert.ok(markup.includes('rv-step-num" aria-hidden="true">' + n + '<'), 'step number ' + n);
assert.ok(markup.includes('each step runs in order'), 'the intro explains the pipeline');

// Workflow, reviewer and merge method are pickers of configured values.
assert.ok(markup.includes('data-picker="review_workflow"'), 'workflow picker');
assert.ok(markup.includes('data-picker="reviewer"'), 'reviewer picker');
assert.ok(markup.includes('<option value="codex"') && markup.includes('<option value="proto"'), 'reviewer lists configured delegates');
assert.ok(markup.includes('data-method="merge_method"'), 'merge-method picker');
assert.ok(markup.includes('Squash and merge') && markup.includes('Create a merge commit') && markup.includes('Rebase and merge'), 'merge method options');

// Dependent cards: a two-way radio with the trade-off.
assert.ok(markup.includes('data-dep-gate="merge"') && markup.includes('data-dep-gate="review"'), 'dep-gate radio renders both values');
assert.ok(markup.includes('When the blocker merges') && markup.includes('safer'), 'trade-off shown');

// No raw config key leaks into the VISIBLE text (data-* attributes live inside tags).
const text = strip(markup);
for (const key of PAGE_OWNED) assert.ok(!text.includes(key), 'raw key leaked as visible copy: ' + key);

// A dependent attempt is live while its step is on, greyed+disabled once the step goes off.
assert.ok(markup.includes('rv-knob" data-knob="review_fix_max"'), 'review_fix_max live while Review on');
assert.ok(!markup.includes('rv-knob--off" data-knob="review_fix_max"'), 'not greyed while on');
listeners.change({target: el(['[data-step-switch]'], {dataset: {stepSwitch: 'review_gate'}, checked: false})});
await flush();
assert.ok(markup.includes('rv-knob--off" data-knob="review_fix_max"'), 'review_fix_max greyed when Review off');
assert.ok(markup.includes('data-step="review_fix_max" data-dir="-1" disabled'), 'its stepper is disabled when greyed');
assert.ok(markup.includes('rv-knob" data-knob="reviewer"'), 'the non-dependent reviewer stays live');

// A LIVE knob (merged_verify_max) steps through PUT /settings and shows a plain "Saved.".
let before = calls.filter(c => c.method === 'PUT').length;
listeners.click({target: el(['[data-step]'], {dataset: {step: 'merged_verify_max', dir: '1'}})});
await flush();
const puts = calls.filter(c => c.method === 'PUT' && c.path.endsWith('/settings'));
assert.deepEqual(puts[puts.length - 1].body, {merged_verify_max: 6}, 'stepping saved through PUT /settings');
assert.ok(markup.includes('data-val="merged_verify_max">6<'), 'stepped value re-rendered');
assert.ok(markup.includes('Saved.') && !markup.includes('restart to apply.'), 'LIVE knob shows plain Saved');

// A restart-only knob (review_dispatch) nudges the host and rides the pending-restart banner.
posted.length = 0;
listeners.change({target: el(['[data-bool]'], {dataset: {bool: 'review_dispatch'}, checked: true})});
await flush();
const dispatchPut = calls.filter(c => c.method === 'PUT' && c.body && 'review_dispatch' in c.body);
assert.deepEqual(dispatchPut[dispatchPut.length - 1].body, {review_dispatch: true}, 'advisory dispatch saved');
const nudge = posted.find(m => m.type === 'protoagent:settings:restart-pending');
assert.ok(nudge && nudge.keys.includes('review_dispatch'), 'restart-pending posted, naming the key');
assert.ok(markup.includes('Saved — restart to apply.'), 'restart knob message');
assert.ok(markup.includes('effect after a restart'), 'pending-restart banner shown');
assert.ok(markup.includes('Request an extra review on opening'), 'banner lists by label');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness + script + assertions],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_live_reviewer_picker_falls_back_to_free_text_without_a_delegate_list():
    """routing/effective can be unreachable or name no delegates; the reviewer control must still
    be settable. Like the Coders coder picker, it falls back to a free-text input whose typed name
    saves through PUT /settings (a board whose reviewer is not in any ladder is not stranded)."""
    script = REVIEW_PAGE.split('<script type="module">', 1)[1].split("</script>", 1)[0]
    script = script.replace('import(BASE + "/_ds/plugin-kit.js")', "globalThis.testKit")
    harness = r"""
import assert from 'node:assert/strict';
const BASE = '';
let markup = '';
const listeners = {};
const host = {
  setAttribute() {}, removeAttribute() {},
  addEventListener(type, fn) { listeners[type] = fn; },
  get firstChild() { return markup ? {} : null; },
  set innerHTML(v) { markup = v; },
  get innerHTML() { return markup; },
};
globalThis.document = { getElementById(id) { return id === 'review' ? host : null; } };
globalThis.window = { parent: { postMessage() {} } };
const META = {
  review_gate: {type: 'bool', restart: true, value: false},
  review_workflow: {type: 'string', restart: true, value: 'code-review'},
  reviewer: {type: 'string', restart: true, value: ''},
  review_dispatch: {type: 'bool', restart: true, value: false},
  review_fix_max: {type: 'number', minimum: 0, maximum: 20, restart: true, value: 2},
  ci_poll: {type: 'bool', restart: true, value: true},
  ci_rerun_max: {type: 'number', minimum: 0, maximum: 5, restart: true, value: 1},
  ci_fix_max: {type: 'number', minimum: 0, maximum: 20, restart: true, value: 2},
  auto_rebase: {type: 'bool', restart: true, value: true},
  rebase_fix_max: {type: 'number', minimum: 0, maximum: 20, restart: true, value: 1},
  auto_merge: {type: 'bool', restart: false, value: false},
  merge_method: {type: 'select', options: ['squash', 'merge', 'rebase'], restart: true, value: 'squash'},
  merged_verify_max: {type: 'number', minimum: 0, maximum: 20, restart: false, value: 5},
  merge_poll: {type: 'bool', restart: true, value: true},
  dep_gate: {type: 'select', options: ['merge', 'review'], restart: true, value: 'merge'},
};
function settings() {
  const s = {};
  for (const [k, m] of Object.entries(META)) s[k] = {...m};
  return {settings: s, pending_restart: []};
}
const calls = [];
globalThis.testKit = {
  initPluginView(cb) { this._boot = cb; },
  async apiFetch(path, init) {
    const method = (init && init.method) || 'GET';
    const body = init && init.body ? JSON.parse(init.body) : null;
    calls.push({path, method, body});
    if (method === 'PUT' && path.endsWith('/settings')) {
      for (const [k, v] of Object.entries(body)) if (META[k]) META[k].value = v;
      return {ok: true, status: 200, async json() { return {ok: true, saved: Object.keys(body), pending_restart: []}; }};
    }
    if (path.endsWith('/routing/effective')) throw new Error('routing unavailable');
    if (path.endsWith('/settings')) return {ok: true, status: 200, async json() { return settings(); }};
    return {ok: true, status: 200, async json() { return {}; }};
  },
};
function el(matches, props) { return Object.assign({closest(s) { return matches.includes(s) ? this : null; }}, props); }
const flush = async () => { for (let i = 0; i < 14; i++) await new Promise(r => setImmediate(r)); };
"""
    assertions = r"""
globalThis.testKit._boot();
await flush();
// No delegate list resolved, so no empty <select> for the reviewer — just a free-text input.
assert.ok(!markup.includes('data-picker="reviewer"'), 'no empty reviewer select without delegates');
assert.ok(markup.includes('data-picker-text="reviewer"'), 'reviewer free-text input offered');
// Typing an agent name saves it through PUT /settings even though no list could confirm it.
listeners.change({target: el(['[data-picker-text]'], {dataset: {pickerText: 'reviewer'}, value: 'quinn'})});
await flush();
const puts = calls.filter(c => c.method === 'PUT' && c.path.endsWith('/settings'));
assert.equal(puts.length, 1, 'one settings PUT');
assert.deepEqual(puts[0].body, {reviewer: 'quinn'}, 'typed reviewer saved through PUT /settings');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness + script + assertions],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
