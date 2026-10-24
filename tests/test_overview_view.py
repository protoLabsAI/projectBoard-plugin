"""Overview Configure tab (S1a, #611): the first Settings tab, answering "is it working?".

Host-free contracts, mirroring test_routing_view / test_projects_view: the page is served on
the PUBLIC router (not under /api), holds no board data, reads and writes only through the DS
plugin-kit's authed fetch after initPluginView, shows a loading line without a boot-fallback
timer, uses DS tokens with no hardcoded colours, and never prints a raw config key as copy.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from project_board.overview_view import OVERVIEW_PAGE

VIEW = Path(__file__).resolve().parent.parent / "view"

# The gated API paths the page reads/writes — all under /api, reached via the kit's authed fetch.
SETTINGS_API = "/api/plugins/project_board/settings"
STATUS_API = "/api/plugins/project_board/status"
FEATURES_API = "/api/plugins/project_board/features"


def _style_block() -> str:
    return OVERVIEW_PAGE.split("<style>", 1)[1].split("</style>", 1)[0]


# ── the single source file assembles ─────────────────────────────────────────────


def test_overview_page_is_one_self_contained_file():
    assert (VIEW / "overview.html").is_file()
    # One file: the markup carries its own <style> and <script type="module"> inline.
    assert OVERVIEW_PAGE == (VIEW / "overview.html").read_text(encoding="utf-8")
    assert "<style>" in OVERVIEW_PAGE and "</style>" in OVERVIEW_PAGE
    assert 'type="module"' in OVERVIEW_PAGE
    # No unresolved splice placeholder (this page is one file — nothing is spliced in).
    assert "_CSS__" not in OVERVIEW_PAGE and "_JS__" not in OVERVIEW_PAGE


# ── served on the PUBLIC router, 200, not under /api, no-store ────────────────────


def test_route_returns_200_and_is_not_under_api():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from project_board.api import build_router

    app = FastAPI()
    app.include_router(build_router({}), prefix="/plugins/project_board")
    client = TestClient(app)
    path = "/plugins/project_board/config/overview"
    assert "/api" not in path  # the page chrome is public; the DATA lives on the /api prefix
    page = client.get(path)
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-store"
    assert page.text == OVERVIEW_PAGE


def test_the_data_router_does_not_serve_the_page():
    # The /api (gated) router owns DATA, never this page — the public router owns the chrome.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from project_board.api import build_data_router

    app = FastAPI()
    app.include_router(build_data_router({}), prefix="/api/plugins/project_board")
    assert TestClient(app).get("/api/plugins/project_board/config/overview").status_code == 404


# ── the page holds NO board data: it all arrives through the authed API ───────────


def test_page_holds_no_data_and_reads_everything_through_the_authed_api():
    # No server-rendered data island — the page is a static template.
    assert '<script type="application/json"' not in OVERVIEW_PAGE
    # Every read is a gated /api path fetched through the kit; none is inlined as a value.
    for api in (SETTINGS_API, STATUS_API, FEATURES_API):
        assert api in OVERVIEW_PAGE
    assert "kit.apiFetch" in OVERVIEW_PAGE


def test_page_uses_the_fleet_safe_authenticated_plugin_kit_after_init():
    assert 'location.pathname.split("/plugins/")[0]' in OVERVIEW_PAGE
    assert 'import(BASE + "/_ds/plugin-kit.js")' in OVERVIEW_PAGE
    assert "/_ds/plugin-kit.css" in OVERVIEW_PAGE
    assert "kit.initPluginView(" in OVERVIEW_PAGE
    # Strict, like Projects: NO unauthenticated bare-fetch fallback that would race the bearer.
    assert "apiFetch: (p, i) => fetch" not in OVERVIEW_PAGE
    assert "Could not load the authenticated plugin bridge" in OVERVIEW_PAGE


def test_boot_is_initpluginview_only_with_no_settimeout_fallback():
    # The one boot path is initPluginView's callback — no timer that would load (or poll) before
    # the authenticated bridge is ready. The only legitimate timer is the slow-read loading line.
    assert "setTimeout(boot" not in OVERVIEW_PAGE
    assert "setTimeout(load" not in OVERVIEW_PAGE
    assert "setTimeout(render" not in OVERVIEW_PAGE
    assert "if (!booted) { booted = true; render(); }" in OVERVIEW_PAGE
    # A loading line only once the reads are actually slow (~0.5s) — no flash on a fast board.
    assert "SLOW_MS = 500" in OVERVIEW_PAGE
    assert "Reading board status…" in OVERVIEW_PAGE


# ── the board switch: loop_enabled, saved through PUT /settings, state in words ───


def test_switch_saves_loop_enabled_through_put_settings_and_states_the_state_in_words():
    assert 'id="board-switch"' in OVERVIEW_PAGE
    assert "pl-switch__input" in OVERVIEW_PAGE
    # Saved through PUT /settings, keyed by the one raw config key the page names (not as copy).
    assert 'const LOOP_KEY = "loop_enabled"' in OVERVIEW_PAGE
    assert 'method: "PUT"' in OVERVIEW_PAGE
    assert "JSON.stringify({ [LOOP_KEY]: on })" in OVERVIEW_PAGE
    # The current state, in plain words — on and off both spelled out.
    assert "On — picking up ready cards." in OVERVIEW_PAGE
    assert "Off — the board and tools still work, nothing is dispatched." in OVERVIEW_PAGE


# ── the counts: each links to the board filtered to that state ────────────────────


def test_counts_link_to_the_board_filtered_to_each_state():
    assert "/plugins/project_board/board" in OVERVIEW_PAGE
    assert 'BOARD + "?state=" + encodeURIComponent(c.state)' in OVERVIEW_PAGE
    # The four lanes, by the board's own state names (in the link + tally, never as visible copy).
    for label, state in (
        ("Building", "in_progress"),
        ("In review", "in_review"),
        ("Blocked", "blocked"),
        ("Waiting", "ready"),
    ):
        assert '{ label: "' + label + '", state: "' + state + '" }' in OVERVIEW_PAGE


# ── the problems: one plain line + a fix (button or sentence), never a raw error alone ─


def test_problems_are_plain_lines_with_a_fix_and_never_a_raw_error_alone():
    assert "<h2>Problems</h2>" in OVERVIEW_PAGE
    # "Re-run check" fix: Re-check re-runs the reads, and the /status read re-runs the setup preflight.
    assert 'id="recheck"' in OVERVIEW_PAGE
    # Each surfaced problem is a plain sentence of what to do — held projects, dirty base, claim stall.
    assert "its ready cards won’t build until they pass again." in OVERVIEW_PAGE
    assert "Reset the board-owned checkout to its base branch" in OVERVIEW_PAGE
    assert "The board has ready work but isn’t starting it" in OVERVIEW_PAGE
    # A failure headline is only ever secondary CONTEXT, never the sole signal.
    assert "ov-problem-title" in OVERVIEW_PAGE and "ov-problem-ctx" in OVERVIEW_PAGE


# ── the pending-restart banner: by label, with how-to-restart text ────────────────


def test_pending_restart_banner_lists_by_label_with_how_to_restart():
    assert "pending_restart" in OVERVIEW_PAGE  # read from the settings snapshot
    assert "effect after a restart" in OVERVIEW_PAGE
    assert "Restart this board member" in OVERVIEW_PAGE
    assert "pl-callout--info" in OVERVIEW_PAGE
    # Listed by the human LABEL, not the raw key.
    assert "p.label || p.key" in OVERVIEW_PAGE


# ── DS tokens only; no hardcoded colours; no raw config key as visible copy ───────


def test_styles_use_ds_tokens_and_no_hardcoded_colours():
    style = _style_block()
    # No id selectors are used in the stylesheet, so any `#` would be a hex colour literal.
    assert "#" not in style
    assert "rgb(" not in style and "rgba(" not in style and "hsl(" not in style
    # Colours come from DS tokens (light + dark).
    assert "var(--pl-color-bg)" in style and "var(--pl-color-fg)" in style
    assert "var(--pl-color-accent)" in style


def test_no_raw_config_key_name_appears_as_visible_copy():
    # `loop_enabled` is referenced ONCE, as the PUT payload key constant — never as rendered copy.
    assert OVERVIEW_PAGE.count("loop_enabled") == 1
    # No other internal knob name leaks into the page at all.
    assert "dep_gate" not in OVERVIEW_PAGE
    # The switch's visible label is operator words, not the key.
    assert "Build ready cards automatically" in OVERVIEW_PAGE


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("health_fails", [False, True])
def test_live_counts_and_repeated_rechecks(health_fails):
    """Run the page with API-shaped rows and buttons replaced on every render."""
    script = OVERVIEW_PAGE.split('<script type="module">', 1)[1].split("</script>", 1)[0]
    script = script.replace('import(BASE + "/_ds/plugin-kit.js")', "globalThis.testKit")
    harness = r"""
import assert from 'node:assert/strict';
const BASE = '';
globalThis.window = {parent: {}, addEventListener() {}};
let button, markup = '', boot, release;
let reads = 0;
const listeners = {};
const host = {
  addEventListener(type, fn) { listeners[type] = fn; },
  set innerHTML(value) {
    markup = value;
    const tag = value.match(/<button[^>]*id="recheck"[^>]*>/);
    button = tag ? {
      disabled: tag[0].includes(' disabled'),
      closest(selector) { return selector === '#recheck' ? this : null; },
    } : null;
  },
};
globalThis.document = {
  getElementById(id) { return id === 'ov' ? host : id === 'recheck' ? button : null; },
};
const rows = ['in_progress', 'in_review', 'in_review', 'blocked', 'blocked', 'blocked',
              'ready', 'ready', 'ready', 'ready', 'done'].map(board_state => ({board_state}));
globalThis.testKit = {
  initPluginView(callback) { boot = callback; },
  async apiFetch(path) {
    reads++;
    if (release) await release.promise;
    if (HEALTH_FAILS && path.endsWith('/status')) throw new Error('unavailable');
    return {ok: true, status: 200, async json() {
      return path.endsWith('/features') ? {features: rows} : {};
    }};
  },
};
""".replace("HEALTH_FAILS", json.dumps(health_fails))
    assertions = r"""
const settled = () => new Promise(resolve => setImmediate(resolve));
boot();
await settled();
assert.deepEqual([...markup.matchAll(/class="ov-count-n">(\d+)</g)].map(m => Number(m[1])),
                 [1, 2, 3, 4]);
assert.equal(button.disabled, false);
for (let attempt = 0; attempt < 2; attempt++) {
  let finish;
  release = {promise: new Promise(resolve => { finish = resolve; })};
  const before = reads;
  listeners.click({target: button});
  assert.equal(button.disabled, true, 'disabled during reads');
  listeners.click({target: button});
  assert.equal(reads, before + 3, 'duplicate clicks do not start more reads');
  finish();
  await settled();
  release = null;
  assert.equal(button.disabled, false, 'replacement button is enabled after reads');
}
assert.equal(reads, 9);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness + script + assertions],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "capabilities",
    [None, {}, {"restart": False}, {"restart": "true"}, {"restart": True}, ["protoagent:restart-request"]],
)
def test_restart_host_contract_and_fresh_settings(capabilities):
    """Exercise the actual module: opt-in, targeted message, reconnect and stale reads."""
    script = OVERVIEW_PAGE.split('<script type="module">', 1)[1].split("</script>", 1)[0]
    script = script.replace('import(BASE + "/_ds/plugin-kit.js")', "globalThis.testKit")
    harness = r"""
import assert from 'node:assert/strict';
const BASE = '/agents/member';
const origin = 'https://console.example';
let message, boot, markup = '', reads = 0, hold = null;
let pending = [{key: 'internal', label: 'Build ready cards automatically'}];
const clicks = {};
const posts = [];
const parent = {postMessage(...args) { posts.push(args); }};
globalThis.window = {parent, addEventListener(type, fn) { message = fn; }};
const host = {
  addEventListener(type, fn) { clicks[type] = fn; },
  set innerHTML(value) { markup = value; },
};
globalThis.document = {getElementById(id) { return id === 'ov' ? host : null; }};
globalThis.testKit = {
  initPluginView(fn) { boot = fn; },
  async apiFetch(path, options) {
    assert.ok(['/api/plugins/project_board/settings', '/api/plugins/project_board/status',
               '/api/plugins/project_board/features'].includes(path), 'never a restart route');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.method, undefined);
    const data = path.endsWith('/settings') ? {pending_restart: [...pending]} : {};
    if (path.endsWith('/settings')) reads++;
    if (hold) await hold;
    return {ok: true, status: 200, async json() { return data; }};
  },
};
const emit = (data, source = parent, from = origin) => message({data, source, origin: from});
const settle = () => new Promise(resolve => setImmediate(resolve));
const clickRestart = () => clicks.click({target: {
  closest(selector) { return selector === '#restart-now' ? this : null; },
}});
"""
    assertions = r"""
// Untrusted frames cannot advertise support or initiate reads.
emit({type: 'protoagent:init', capabilities: {restart: true}}, {});
assert.equal(reads, 0);
emit({type: 'protoagent:init', capabilities: {restart: true}}, parent, 'null');
assert.equal(reads, 0);
emit({type: 'protoagent:init', capabilities: CAPS});
boot();
await settle();
const supported = CAPS?.restart === true || Array.isArray(CAPS);
assert.equal(markup.includes('id="restart-now"'), supported);
assert.equal(markup.includes('Restart this board member'), !supported);
assert.ok(markup.includes('Build ready cards automatically'));
clickRestart();
assert.deepEqual(posts, supported ? [[{type: 'protoagent:restart-request'}, origin]] : []);
assert.ok(markup.includes('effect after a restart'), 'request does not clear pending');
const before = reads;
emit({type: 'protoagent:reconnected'}, {});
emit({type: 'protoagent:reconnected'}, parent, 'https://other.example');
assert.equal(reads, before);
// An old pending snapshot must not overwrite the fresh post-reconnect read.
let release;
hold = new Promise(resolve => { release = resolve; });
emit({type: 'protoagent:reconnected'});
hold = null;
pending = [];
emit({type: 'protoagent:reconnected'});
await settle();
assert.equal(reads, before + 2);
assert.ok(!markup.includes('effect after a restart'));
release();
await settle();
assert.ok(!markup.includes('effect after a restart'));
// Repeated init also refreshes settings and can withdraw support.
pending = [{label: 'Another change'}];
emit({type: 'protoagent:init'});
await settle();
assert.equal(reads, before + 3);
assert.ok(markup.includes('Restart this board member'));
assert.ok(!markup.includes('id="restart-now"'));
""".replace("CAPS", json.dumps(capabilities))
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness + script + assertions],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
