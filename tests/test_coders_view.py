"""Configure ▸ Coders (S3, #613): the routing tab grows a default-coder picker, LIVE capacity
steppers with a consequence line, and a collapsed Advanced block of tuning knobs.

Host-free contracts in the shape of test_routing_view / test_overview_view: the page is served
on the PUBLIC router (tested there), holds no board data, reads and writes only through the DS
plugin-kit's authed fetch after initPluginView, uses DS tokens with no hardcoded colours, and
never prints a raw config key as visible copy. The scalar knobs save through the guarded PUT
/settings boundary (S0); the allow/deny switches and ladder keep B3's PUT /routing. A node
harness exercises the interactive bits (the picker save, its free-text fallback so a ladderless
board can still set a coder, the inline refusal, the stepper's consequence line updating, and the
restart-pending host nudge)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from project_board.routing_view import ROUTING_PAGE

VIEW = Path(__file__).resolve().parent.parent / "view"

# The nine scalar knobs the Coders tab now owns, by raw key — used for the "no raw key as
# visible copy" sweep, never shown to an operator.
PAGE_OWNED = (
    "coder",
    "max_concurrent",
    "max_pending_reviews",
    "max_concurrent_sessions",
    "coder_solve",
    "coder_solve_test_cmd",
    "max_mode_n",
    "decompose_after_timeouts",
    "goal_verify",
)
ADVANCED_LABELS = (
    "Parallel coding sessions per build",
    "Try solutions until tests pass",
    "Tests for candidate solutions",
    "Alternative builds to compare",
    "Timeouts before asking to split a card",
    "Check acceptance criteria before review",
)


def _css() -> str:
    return (VIEW / "routing.css").read_text(encoding="utf-8")


# ── the page contract: authed kit after init, both reads, no boot-timer fallback ─────────────


def test_page_reads_both_apis_through_the_authed_kit_after_init():
    assert 'import(BASE + "/_ds/plugin-kit.js")' in ROUTING_PAGE
    assert "kit.initPluginView" in ROUTING_PAGE and "kit.apiFetch" in ROUTING_PAGE
    assert "/_ds/plugin-kit.css" in ROUTING_PAGE
    # Coders + ladder from /routing/effective; the scalar knobs from /settings.
    assert '"/api/plugins/project_board/routing/effective"' in ROUTING_PAGE
    assert '"/api/plugins/project_board/settings"' in ROUTING_PAGE
    # The one boot path is initPluginView's callback — no timer that would load before the bearer.
    assert "setTimeout(() => { if (!booted)" not in ROUTING_PAGE
    assert "if (!booted){ booted = true; renderRouting(); }" in ROUTING_PAGE


def test_allow_deny_switch_still_saves_through_put_routing():
    # The B3 coder allow/deny switch is unchanged: all-on clears the exclusive; one must stay on.
    assert 'addEventListener("change"' in ROUTING_PAGE
    assert "routingSave(on.length === all.length ? [] : on)" in ROUTING_PAGE
    assert "At least one coder has to build." in ROUTING_PAGE


# ── the default-coder picker: configured delegates, marks default, PUT /settings, inline refusal ─


def test_coder_picker_lists_configured_delegates_and_saves_through_put_settings():
    assert 'id="coder-picker"' in ROUTING_PAGE
    # Options come from the delegates the routing read returns — not a hand-kept list.
    assert "ROUTING_BODY && ROUTING_BODY.delegates" in ROUTING_PAGE
    # Saved through the guarded settings route, keyed by `coder`.
    assert "putSettings({[key]: value})" in ROUTING_PAGE
    assert 'saveSetting("coder", name)' in ROUTING_PAGE
    # The chosen coder is marked on its card.
    assert "r-coder--default" in ROUTING_PAGE and ">default<" in ROUTING_PAGE


def test_invalid_coder_choice_is_refused_inline_and_never_submitted():
    # A value that comes straight off the <select> can only be a configured delegate; chooseCoder
    # still guards that path, refusing an unconfigured one inline with no PUT.
    assert "function chooseCoder(name, typed)" in ROUTING_PAGE
    assert "valid.has(name)" in ROUTING_PAGE
    assert "isn’t a configured coder. Pick one from the list" in ROUTING_PAGE


def test_coder_picker_keeps_a_free_text_fallback_so_a_ladderless_board_can_be_set():
    # The routing read only names the delegates in a ladder (plus the current coder), so a picker
    # of just those would strand a fresh board — no ladder means no option and the required coder
    # could never be set, nor could a configured delegate that no ladder names. The control always
    # offers free text: a "Type a coder name…" option and a text input for any delegate the host
    # provides.
    assert '"__custom__"' in ROUTING_PAGE
    assert "Type a coder name…" in ROUTING_PAGE
    assert "data-coder-text" in ROUTING_PAGE
    # A typed name is NOT refused for being absent from the ladder-derived list — it's saved and
    # setup checks confirm it resolves. Only an empty typed name is refused inline.
    assert "chooseCoder(coderText.value, true)" in ROUTING_PAGE
    assert "Enter a coder name — the loop needs one to build." in ROUTING_PAGE
    # With nothing to pick from, no empty <select> is rendered — only the text input.
    assert "if (dels.length)\n    control +=" in ROUTING_PAGE


# ── capacity: two LIVE steppers, each with a one-line consequence that updates ────────────────


def test_capacity_steppers_have_bounds_a_consequence_and_live_save():
    for key in ("max_concurrent", "max_pending_reviews"):
        assert 'data-step="' + key + '"' not in ROUTING_PAGE  # built via esc(key) at runtime
    # The steppers clamp to the snapshot's manifest min/max and consequence lines are present.
    assert "Math.max(meta.minimum, Math.min(meta.maximum" in ROUTING_PAGE
    assert "Up to " in ROUTING_PAGE and "cards build at once." in ROUTING_PAGE
    assert "pull request" in ROUTING_PAGE and "wait" in ROUTING_PAGE
    # LIVE knobs show a plain "Saved."; restart-only knobs say "restart to apply." instead.
    assert '"Saved."' in ROUTING_PAGE
    assert '"Saved — restart to apply."' in ROUTING_PAGE


# ── Advanced: collapsed <details>, the six tuning knobs, plain labels + one sentence each ─────


def test_advanced_block_is_collapsed_by_default_with_the_six_tuning_knobs():
    # A <details> with no `open` attribute is collapsed.
    assert '<details class="r-sec r-advanced">' in ROUTING_PAGE
    assert "<details open" not in ROUTING_PAGE and 'r-advanced" open' not in ROUTING_PAGE
    for label in ADVANCED_LABELS:
        assert label in ROUTING_PAGE
    # The candidate-test command is monospace + wrapped.
    assert 'kind:"text", mono:true' in ROUTING_PAGE
    assert ".r-mono{font-family:var(--pl-font-mono)" in _css()


def test_restart_only_knobs_nudge_the_host_and_ride_the_banner():
    # Restart-only saves post the (pre-#4489, harmless) host message and show a pending banner.
    assert "window.parent.postMessage(" in ROUTING_PAGE
    assert '"protoagent:settings:restart-pending"' in ROUTING_PAGE
    assert "pending_restart" in ROUTING_PAGE
    assert "effect after a restart" in ROUTING_PAGE
    assert "Restart this board member" in ROUTING_PAGE


# ── DS tokens only; no hardcoded colours; no raw config key as visible copy ───────────────────


def test_styles_use_ds_tokens_and_no_hardcoded_hex_colours():
    import re

    style = _css()
    rules = re.sub(r"/\*.*?\*/", "", style, flags=re.S)  # drop comments (they carry `#<issue>`)
    # No id selectors are used, so any `#` left in the rules would be a hex colour literal.
    assert "#" not in rules
    assert ":root{" not in rules and ":root {" not in rules
    # Colours come from DS tokens (light + dark). The only rgba() literals are the kit tokens'
    # own fallbacks inside var(--…, rgba(…)) — never standalone chrome.
    assert "var(--pl-color-bg)" in style and "var(--pl-color-fg)" in style
    assert "var(--pl-color-accent)" in style
    for match in re.finditer(r"rgba?\(", rules):
        head = rules[: match.start()]
        assert head.rstrip().endswith(","), "rgba() must only appear as a var() token fallback"


def test_advanced_labels_are_plain_words_not_raw_keys():
    # The visible Advanced labels never leak an internal key name.
    for label in ADVANCED_LABELS:
        assert "_" not in label and "." not in label


def test_all_nine_page_owned_keys_are_wired_into_the_page():
    # Every key this tab took over the Automation list for is handled by the page (as a picker,
    # stepper or advanced knob) — so none silently stopped being configurable in the move.
    for key in PAGE_OWNED:
        assert key in ROUTING_PAGE, key


# ── the live behaviours: picker save, stepper consequence, invalid refusal, restart nudge ─────


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_live_picker_stepper_and_restart_nudge():
    """Run the real page script with API-shaped reads and a stubbed host frame."""
    script = ROUTING_PAGE.split('<script type="module">', 1)[1].split("</script>", 1)[0]
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
  querySelectorAll() { return []; },
  get firstChild() { return markup ? {} : null; },
  set innerHTML(v) { markup = v; },
  get innerHTML() { return markup; },
};
globalThis.document = { getElementById(id) { return id === 'routing' ? host : null; } };
globalThis.window = { parent: { postMessage(m) { posted.push(m); } } };

const ROUTING = {
  delegates: ['codex', 'proto'],
  board_exclusive: [],
  cursor: 0,
  projects: {
    app: {
      escalation_on: true,
      delegates: [{name: 'codex', provider_is_down: false, headroom: 'unknown'},
                  {name: 'proto', provider_is_down: false, headroom: 'unknown'}],
      ladder: [{tier: 'standard', delegates: ['proto'], starts_on: 'proto', reason: 'round-robin', policy: {}}],
      usage_policy: [],
    },
  },
};
const META = {
  coder: {type: 'string', restart: false, value: 'codex'},
  max_concurrent: {type: 'number', minimum: 1, maximum: 64, restart: false, value: 1},
  max_pending_reviews: {type: 'number', minimum: 0, maximum: 1000, restart: false, value: 5},
  max_concurrent_sessions: {type: 'number', minimum: 0, maximum: 32, restart: false, value: 0},
  coder_solve: {type: 'bool', restart: true, value: true, label: 'Try solutions until tests pass'},
  coder_solve_test_cmd: {type: 'string', restart: true, value: ''},
  max_mode_n: {type: 'number', minimum: 1, maximum: 16, restart: true, value: 1},
  decompose_after_timeouts: {type: 'number', minimum: 0, maximum: 10, restart: true, value: 2},
  goal_verify: {type: 'bool', restart: true, value: false, label: 'Check acceptance criteria before review'},
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
    if (path.endsWith('/routing/effective')) return {ok: true, status: 200, async json() { return ROUTING; }};
    if (path.endsWith('/settings')) return {ok: true, status: 200, async json() { return settings(); }};
    return {ok: true, status: 200, async json() { return {}; }};
  },
};
function el(matches, props) { return Object.assign({closest(s) { return matches.includes(s) ? this : null; }}, props); }
const flush = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setImmediate(r)); };
const strip = (s) => s.replace(/<[^>]*>/g, ' ');
"""
    assertions = r"""
globalThis.testKit._boot();
await flush();

// Picker lists configured delegates and marks the default on its card.
assert.ok(markup.includes('id="coder-picker"'), 'picker rendered');
assert.ok(markup.includes('<option value="codex" selected>codex</option>'), 'codex selectable');
assert.ok(markup.includes('<option value="proto"'), 'proto selectable');
assert.ok(markup.includes('r-coder--default'), 'default card marked');

// Advanced is collapsed by default.
assert.ok(markup.includes('<details class="r-sec r-advanced">'), 'advanced present');
assert.ok(!/<details[^>]*\bopen\b/.test(markup), 'advanced collapsed');

// No raw config key leaks into the VISIBLE text (data-* attributes live inside tags).
const text = strip(markup);
for (const key of ['max_mode_n', 'coder_solve_test_cmd', 'decompose_after_timeouts',
                   'max_pending_reviews', 'max_concurrent', 'goal_verify']) {
  assert.ok(!text.includes(key), 'raw key leaked as visible copy: ' + key);
}

// Consequence line reflects the current value and updates when stepped.
assert.ok(markup.includes('One card builds at a time.'), 'consequence at value 1');
listeners.click({target: el(['[data-step]'], {dataset: {step: 'max_concurrent', dir: '1'}})});
await flush();
const puts = calls.filter(c => c.method === 'PUT' && c.path.endsWith('/settings'));
assert.deepEqual(puts[0].body, {max_concurrent: 2}, 'stepping saved through PUT /settings');
assert.ok(markup.includes('Up to 2 cards build at once.'), 'consequence updated');
assert.ok(markup.includes('Saved.'), 'LIVE knob shows Saved');

// An invalid coder choice is refused inline, with no PUT.
const beforeCoder = calls.filter(c => c.body && 'coder' in c.body).length;
listeners.change({target: el(['#coder-picker'], {value: 'ghost'})});
await flush();
assert.ok(markup.includes('isn’t a configured coder'), 'invalid refused inline');
assert.equal(calls.filter(c => c.body && 'coder' in c.body).length, beforeCoder, 'no PUT for invalid coder');

// A restart-only knob nudges the host and rides the pending-restart banner.
posted.length = 0;
listeners.change({target: el(['input[data-bool]'], {dataset: {bool: 'goal_verify'}, checked: true})});
await flush();
const nudge = posted.find(m => m.type === 'protoagent:settings:restart-pending');
assert.ok(nudge, 'restart-pending posted to host');
assert.ok(nudge.keys.includes('goal_verify'), 'nudge names the key');
assert.ok(markup.includes('Saved — restart to apply.'), 'restart knob message');
assert.ok(markup.includes('effect after a restart'), 'pending-restart banner shown');
assert.ok(markup.includes('Check acceptance criteria before review'), 'banner lists by label');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness + script + assertions],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_live_ladderless_board_can_still_set_its_coder_by_free_text():
    """A board with no ladder resolves no delegates, so the picker has nothing to list. It must
    still be able to set the required coder: the control falls back to a free-text input and the
    typed name saves through PUT /settings (a regression guard for the review that rejected a
    picker-only control, which stranded a fresh board)."""
    script = ROUTING_PAGE.split('<script type="module">', 1)[1].split("</script>", 1)[0]
    script = script.replace('import(BASE + "/_ds/plugin-kit.js")', "globalThis.testKit")
    harness = r"""
import assert from 'node:assert/strict';
const BASE = '';
let markup = '';
const listeners = {};
const host = {
  setAttribute() {}, removeAttribute() {},
  addEventListener(type, fn) { listeners[type] = fn; },
  querySelectorAll() { return []; },
  get firstChild() { return markup ? {} : null; },
  set innerHTML(v) { markup = v; },
  get innerHTML() { return markup; },
};
globalThis.document = {
  getElementById(id) {
    if (id === 'routing') return host;
    if (id === 'coder-text') return {focus() {}};
    return null;
  },
};
globalThis.window = { parent: { postMessage() {} } };

// Fresh board: no ladder, so routing resolves no delegates, and the coder is unset.
const ROUTING = {delegates: [], board_exclusive: [], cursor: 0, projects: {}};
const META = {
  coder: {type: 'string', restart: false, value: ''},
  max_concurrent: {type: 'number', minimum: 1, maximum: 64, restart: false, value: 1},
  max_pending_reviews: {type: 'number', minimum: 0, maximum: 1000, restart: false, value: 5},
  max_concurrent_sessions: {type: 'number', minimum: 0, maximum: 32, restart: false, value: 0},
  coder_solve: {type: 'bool', restart: true, value: true},
  coder_solve_test_cmd: {type: 'string', restart: true, value: ''},
  max_mode_n: {type: 'number', minimum: 1, maximum: 16, restart: true, value: 1},
  decompose_after_timeouts: {type: 'number', minimum: 0, maximum: 10, restart: true, value: 2},
  goal_verify: {type: 'bool', restart: true, value: false},
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
    if (path.endsWith('/routing/effective')) return {ok: true, status: 200, async json() { return ROUTING; }};
    if (path.endsWith('/settings')) return {ok: true, status: 200, async json() { return settings(); }};
    return {ok: true, status: 200, async json() { return {}; }};
  },
};
function el(matches, props) { return Object.assign({closest(s) { return matches.includes(s) ? this : null; }}, props); }
const flush = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setImmediate(r)); };
"""
    assertions = r"""
globalThis.testKit._boot();
await flush();

// No ladder => no <select> picker, but a free-text input is offered so the coder is still settable.
assert.ok(!markup.includes('id="coder-picker"'), 'no empty select on a ladderless board');
assert.ok(markup.includes('data-coder-text'), 'free-text coder input offered');

// Typing a delegate name saves it through PUT /settings — the name is not refused for being
// absent from the (empty) ladder-derived list.
listeners.change({target: el(['[data-coder-text]'], {value: 'codex'})});
await flush();
const puts = calls.filter(c => c.method === 'PUT' && c.path.endsWith('/settings'));
assert.equal(puts.length, 1, 'one settings PUT');
assert.deepEqual(puts[0].body, {coder: 'codex'}, 'typed coder saved through PUT /settings');
assert.ok(markup.includes('Saved.'), 'inline Saved shown');

// An empty (whitespace) name is refused inline, with no further PUT.
listeners.change({target: el(['[data-coder-text]'], {value: '   '})});
await flush();
assert.equal(calls.filter(c => c.method === 'PUT' && c.path.endsWith('/settings')).length, 1, 'blank name not submitted');
assert.ok(markup.includes('Enter a coder name'), 'blank refused inline');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness + script + assertions],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
