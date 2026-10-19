"""The Kanban moves: keyed render + FLIP motion, live refresh, operator drag/keyboard moves.

Three tiers, all host-free:

* structural — the page source carries the seams (keyed node map, FLIP pass, the
  reduced-motion branch, pointer-event DnD and never HTML5 DnD, the keyboard twin);
* behavioural, under node — the pure move planner (``planMove`` / ``dropPriority`` /
  ``dropLanes`` / ``menuItems``) and the optimistic-patch sort are extracted from the
  assembled page and run against real feature rows;
* API — every move verb maps to a route that exists, ``GET /rev`` moves on a store write,
  and ``GET /features`` answers a matching ``If-None-Match`` with an empty 304.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_board import api
from project_board.board_view import BOARD_PAGE

node = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


# ── structural: the seams ─────────────────────────────────────────────────────────


def test_kanban_is_a_keyed_render_not_an_innerhtml_rebuild():
    """One node per card id, reused across renders — the old whole-board innerHTML
    rebuild (which made animation impossible) is gone from the Kanban path."""
    assert "const CARD_NODES = new Map();" in BOARD_PAGE
    assert "function renderKanban()" in BOARD_PAGE
    assert '$("kanban").innerHTML = COLS.map' not in BOARD_PAGE
    body = BOARD_PAGE[BOARD_PAGE.index("function renderKanban()") : BOARD_PAGE.index("function flip(")]
    assert "CARD_NODES.get(f.id)" in body
    assert "list.insertBefore(n, list.children[i] || null)" in body
    # a card's markup is rewritten only when it changed
    assert "if (n._html !== html){ n.innerHTML = html; n._html = html; }" in BOARD_PAGE
    # the lane skeleton is built once
    assert "if (root.dataset.keyed) return;" in BOARD_PAGE


def test_flip_hook_measures_inverts_and_plays():
    body = BOARD_PAGE[BOARD_PAGE.index("function flip(") : BOARD_PAGE.index("function leave(")]
    assert "getBoundingClientRect()" in body
    assert 'm.n.style.transform = "translate("+m.dx+"px,"+m.dy+"px)";' in body  # invert
    assert "void document.body.offsetWidth;" in body  # commit the inverted frame
    assert '"transform "+MOVE_MS+"ms "+EASE+" "+delay+"ms"' in body  # play, staggered
    assert "Math.min(k++, STAGGER_MAX) * STAGGER_MS" in body
    # first rects are taken BEFORE the DOM mutation in renderKanban
    rk = BOARD_PAGE[BOARD_PAGE.index("function renderKanban()") : BOARD_PAGE.index("function flip(")]
    assert rk.index("first.set(id, n.getBoundingClientRect())") < rk.index("updateCard(n, f, state)")
    assert "const MOVE_MS = 440" in BOARD_PAGE  # inside the 350-500 ms brief


def test_enter_leave_land_and_count_tick_exist():
    for cls in ("card--enter", "card--leave", "card--landed", "lane-n--tick"):
        assert cls in BOARD_PAGE
    assert "@keyframes card-in" in BOARD_PAGE and "@keyframes card-land" in BOARD_PAGE
    assert 'if (state === "done") landed.push(n);' in BOARD_PAGE  # the done moment, once per arrival


def test_reduced_motion_branch_skips_movement_and_flashes_instead():
    assert 'window.matchMedia("(prefers-reduced-motion: reduce)")' in BOARD_PAGE
    assert "const animate = motionOK() && !FIRST_PAINT" in BOARD_PAGE
    assert 'else if (!FIRST_PAINT) entered.concat(changed).forEach(n => restartClass(n, "card--flash"));' in BOARD_PAGE
    # the drag's helpers honour it too
    assert "function flipAround(mutate){\n  if (!motionOK()){ mutate(); return; }" in BOARD_PAGE
    css = BOARD_PAGE[BOARD_PAGE.index("@media (prefers-reduced-motion:reduce){\n    .lane[") :]
    css = css[: css.index("}\n  }") + 4]
    for cls in (".card--enter", ".card--leave", ".card--landed", ".bld-dot"):
        assert cls in css


def test_drag_and_drop_uses_pointer_events_never_html5_dnd():
    """Tauri (the desktop app) swallows HTML5 drag-and-drop, so the board drags on pointer
    events only."""
    for ev in ("pointerdown", "pointermove", "pointerup", "pointercancel"):
        assert f'document.addEventListener("{ev}"' in BOARD_PAGE
    for html5 in ("draggable", "dragstart", "dragover", "ondrop", "dataTransfer"):
        assert html5 not in BOARD_PAGE
    assert "n.setPointerCapture(e.pointerId)" in BOARD_PAGE
    assert "const DRAG_THRESHOLD = 6;" in BOARD_PAGE
    # a drag is never also a click on the card
    assert "SUPPRESS_CLICK = true;" in BOARD_PAGE


def test_loop_owned_lanes_are_read_only_and_say_so():
    assert 'const LOOP_OWNED = new Set(["in_progress", "in_review", "done"]);' in BOARD_PAGE
    assert "if (owned) n.title = LOOP_WHY[state];" in BOARD_PAGE  # the tooltip
    assert "if (LOOP_OWNED.has(f.state)){ toast(LOOP_WHY[f.state]" in BOARD_PAGE  # a refused drag says why
    assert 'class="lane-lock" title="' in BOARD_PAGE


def test_every_drag_has_a_keyboard_twin():
    assert "function openMenu(fid, anchor)" in BOARD_PAGE
    assert 'e.key === "m" || e.key === "M" || e.key === "ContextMenu"' in BOARD_PAGE
    assert '{ArrowRight: "ready", ArrowUp: "up", ArrowDown: "down"}[e.key]' in BOARD_PAGE
    assert "n.tabIndex = 0;" in BOARD_PAGE
    assert 'id="move-menu" role="menu"' in BOARD_PAGE
    # Esc unwinds a drag, then the menu, then the drawer
    assert "if (DRAG) { endDrag(false); return; }\n  if (closeMenu(true)) return;\n  closeMonitor();" in BOARD_PAGE


def test_moves_are_optimistic_with_rollback():
    body = BOARD_PAGE[BOARD_PAGE.index("async function runMove(") : BOARD_PAGE.index("// ── Toasts")]
    assert body.index("render();") < body.index("await apiPost(")  # shows before the call
    assert "FEATURES = prev;                          // roll back" in body
    assert "PENDING.delete(fid);" in body
    # an in-flight patch survives a poll
    assert "FEATURES = withPending(sortFeatures(" in BOARD_PAGE


def test_live_refresh_polls_the_change_stamp_with_an_etag_full_read():
    assert 'kit.apiFetch("/api/plugins/project_board/rev")' in BOARD_PAGE
    assert "const REV_POLL_MS = 2000, FULL_REFRESH_MS = 15000, SLOW_POLL_MS = 10000;" in BOARD_PAGE
    assert '{headers: {"if-none-match": FEATURES_ETAG}}' in BOARD_PAGE
    assert "if (res.status !== 304){" in BOARD_PAGE
    assert "if (r.status === 404) REV_OK = false;" in BOARD_PAGE  # older plugin → slow poll
    assert 'document.addEventListener("visibilitychange"' in BOARD_PAGE
    # a poll that lands mid-drag waits for the drop
    assert "if (DRAG){ RENDER_DEFERRED = true; return; }" in BOARD_PAGE


def test_building_strip_reads_progress_and_ticks_without_rebuilding():
    assert "async function pollBuilding()" in BOARD_PAGE
    assert 'FEAT + encodeURIComponent(fid) + "/progress"' in BOARD_PAGE
    # built once, then only text changes (so the shimmer never restarts)
    assert "if (!b.firstChild) b.innerHTML = " in BOARD_PAGE
    assert 'setText(b.querySelector(".bld-el"), i.el);' in BOARD_PAGE


def test_kanban_css_uses_tokens_only():
    css_start = BOARD_PAGE.index("/* ── Kanban — no DS primitive")
    css = BOARD_PAGE[css_start : BOARD_PAGE.index("@media (prefers-reduced-motion:reduce){.live::before")]
    assert "#" not in css.replace("#fx", "").replace("#move-menu", ""), "no hex colours"
    assert "rgba(" not in css and "rgb(" not in css


# ── behavioural, under node ───────────────────────────────────────────────────────


def _block(prefix: str) -> str:
    """The statement starting at ``prefix``: a balanced {…} block, or one line."""
    start = BOARD_PAGE.index(prefix)
    line_end = BOARD_PAGE.index("\n", start)
    first = BOARD_PAGE[start:line_end]
    if "{" not in first or first.count("{") == first.count("}"):
        return first
    depth, i = 0, BOARD_PAGE.index("{", start)
    while True:
        depth += BOARD_PAGE[i] == "{"
        depth -= BOARD_PAGE[i] == "}"
        i += 1
        if depth == 0:
            return BOARD_PAGE[start : BOARD_PAGE.index("\n", i)]


def _run(expr: str):
    js = "\n".join(
        [
            _block("const COLS = "),
            _block("const STATE_LABEL = {"),
            _block("const TASK_TYPE"),
            _block("const isTask"),
            _block("const LOOP_OWNED"),
            _block("const LOOP_WHY = {"),
            _block("const PRIORITY_MIN"),
            _block("const MOVES = {"),
            _block("const NO_UNREADY"),
            "let PENDING = new Map();",
            *(
                _block(f"function {n}(")
                for n in (
                    "planMove",
                    "dropPriority",
                    "dropLanes",
                    "menuItems",
                    "planFor",
                    "sortFeatures",
                    "withPending",
                )
            ),
            f"console.log(JSON.stringify({expr}));",
        ]
    )
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _f(fid, state, pri=2, **kw):
    return {"id": fid, "state": state, "priority": pri, "blocked": False, **kw}


@node
def test_allowed_transition_map():
    """backlog → on deck marks ready; reorders inside backlog/on deck change priority;
    everything else is refused with a reason."""
    card = json.dumps(_f("bd-a", "backlog"))
    got = _run(
        f"(() => {{ const f = {card}; return {{lanes: dropLanes(f),"
        " toReady: planMove(f, 'ready', [], 0),"
        " toBuilding: planMove(f, 'in_progress', [], 0),"
        " toDone: planMove(f, 'done', [], 0)}; })()"
    )
    assert got["lanes"] == ["backlog", "ready"]
    assert got["toReady"] == {"ok": True, "verb": "ready"}
    assert got["toBuilding"]["ok"] is False and "build loop" in got["toBuilding"]["why"]
    assert got["toDone"]["ok"] is False


@node
def test_ready_back_to_backlog_is_refused_no_unready_route():
    got = _run("planMove({id:'bd-r',state:'ready',priority:2}, 'backlog', [], 0)")
    assert got["ok"] is False and "no un-ready action" in got["why"]


@node
@pytest.mark.parametrize("state", ["in_progress", "in_review", "done"])
def test_loop_owned_cards_cannot_move_anywhere(state):
    got = _run(
        f"(() => {{ const f = {{id:'bd-x',state:'{state}',priority:2}};"
        " return [dropLanes(f), ...['backlog','ready','in_progress','in_review','done'].map(t => planMove(f, t, [], 0).ok)]; })()"
    )
    assert got[0] == []
    assert got[1:] == [False] * 5


@node
def test_reorder_takes_the_neighbours_priority():
    lane = json.dumps([_f("bd-1", "ready", 0), _f("bd-2", "ready", 1), _f("bd-3", "ready", 3)])
    me = json.dumps(_f("bd-me", "ready", 2))
    got = _run(
        f"(() => {{ const L = {lane}, f = {me}; return ["
        " planMove(f, 'ready', L, 0),"  # top: above P0 → P0
        " planMove(f, 'ready', L, 1),"  # between P0 and P1 → P1
        " planMove(f, 'ready', L, 3),"  # end: after P3 → P3
        " planMove(f, 'ready', L, 2)];"  # between P1 and P3 — where it already sorts → no-op
        " })()"
    )
    assert [g.get("priority") for g in got[:3]] == [0, 1, 3]
    assert all(g["verb"] == "priority" for g in got[:3])
    assert got[3]["ok"] is False and got[3]["noop"] is True


@node
def test_drop_into_ready_carries_the_priority_of_the_slot():
    lane = json.dumps([_f("bd-1", "ready", 1), _f("bd-2", "ready", 2)])
    me = json.dumps(_f("bd-me", "backlog", 3))
    got = _run(f"planMove({me}, 'ready', {lane}, 0)")
    assert got == {"ok": True, "verb": "ready", "priority": 1}


@node
def test_priority_moves_clamp_to_the_store_range():
    got = _run(
        "[planFor({id:'a',state:'ready',priority:0}, 'up'), planFor({id:'a',state:'ready',priority:4}, 'down'),"
        " planFor({id:'a',state:'backlog',priority:2}, 'up')]"
    )
    assert got[0]["ok"] is False and got[1]["ok"] is False
    assert got[2] == {"ok": True, "verb": "priority", "priority": 1}


@node
def test_menu_offers_only_real_actions_and_explains_the_rest():
    got = _run(
        "[menuItems({id:'a',state:'backlog',priority:2,blocked:false}).map(i => i.id + (i.disabled ? '!' : '')),"
        " menuItems({id:'b',state:'in_review',priority:2,blocked:true}).map(i => i.id + (i.disabled ? '!' : '')),"
        " menuItems({id:'c',state:'ready',priority:2,blocked:false}).map(i => i.id + (i.disabled ? '!' : ''))]"
    )
    assert got[0] == ["ready", "up", "down", "block"]
    assert got[1] == ["owned!", "unblock"]
    assert got[2] == ["up", "down", "unready!", "block"]


@node
def test_pending_patch_survives_a_poll_and_resorts():
    rows = json.dumps([_f("bd-1", "backlog", 1), _f("bd-2", "ready", 2)])
    got = _run(
        f"(() => {{ PENDING.set('bd-1', {{state:'ready', board_state:'ready'}});"
        f" return withPending({rows}).map(f => f.id + ':' + f.state); }})()"
    )
    assert got == ["bd-1:ready", "bd-2:ready"]


# ── API: every verb has a route; /rev; the ETag ───────────────────────────────────


def _routes():
    return {(m, r.path) for r in api.build_data_router({}).routes for m in getattr(r, "methods", ())}


def test_every_move_verb_maps_to_a_route_the_api_already_has():
    routes = _routes()
    assert ("POST", "/features/{fid}/ready") in routes  # backlog → on deck
    assert ("PATCH", "/features/{fid}") in routes  # reorder = priority
    assert ("POST", "/features/{fid}/block") in routes
    assert ("POST", "/features/{fid}/unblock") in routes
    for verb in (
        'url + "/ready"',
        'url + "/block"',
        'url + "/unblock"',
        'apiSend("PATCH", url, {priority: plan.priority})',
    ):
        assert verb in BOARD_PAGE
    # the page never calls a transition the loop owns
    for owned in ('"/done"', '"/claim"', '"/attach-pr"', '"/salvage"'):
        assert owned not in BOARD_PAGE


class _Store:
    def __init__(self, db):
        self.db = db
        self.rows = [{"id": "bd-1", "title": "T", "board_state": "ready", "priority": 2}]

    def list_features(self, state=None, include_archived=False):
        return [dict(r) for r in self.rows]


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / ".beads" / "beads.db"
    db.parent.mkdir()
    db.write_bytes(b"x")
    store = _Store(str(db))
    monkeypatch.setattr(api, "get_store", lambda **_kw: store)
    monkeypatch.setattr(api, "annotate_next_action", lambda feats, cfg: feats)
    app = FastAPI()
    app.include_router(api.build_data_router({"db_path": str(db)}), prefix="/api/plugins/project_board")
    return TestClient(app), store, db


def test_rev_moves_when_the_store_is_written(client):
    c, _store, db = client
    a = c.get("/api/plugins/project_board/rev").json()["rev"]
    assert a == c.get("/api/plugins/project_board/rev").json()["rev"]  # stable when idle
    (db.parent / "issues.jsonl").write_text("{}\n")  # br exports on every write
    b = c.get("/api/plugins/project_board/rev").json()["rev"]
    assert b != a
    with open(str(db) + "-wal", "wb") as fh:
        fh.write(b"wal")
    os.utime(str(db) + "-wal", ns=(1, 1))
    assert c.get("/api/plugins/project_board/rev").json()["rev"] != b


def test_features_etag_answers_304_when_unchanged(client):
    c, store, _db = client
    r = c.get("/api/plugins/project_board/features")
    tag = r.headers["etag"]
    assert r.status_code == 200 and tag.startswith('"')
    again = c.get("/api/plugins/project_board/features", headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.content == b""
    store.rows[0]["priority"] = 1
    changed = c.get("/api/plugins/project_board/features", headers={"If-None-Match": tag})
    assert changed.status_code == 200 and changed.headers["etag"] != tag
    assert changed.json()["features"][0]["priority"] == 1
