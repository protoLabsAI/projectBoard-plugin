"""First-load skeletons on the board view.

Before the first ``/features`` response the board's ``FEATURES`` is ``[]`` meaning "not
read yet", and every lane used to paint its EMPTY state ("Nothing in the backlog.") with
a 0 count — on a board that takes a second or more to list, that reads as broken. The
view now paints DS skeleton cards until the first successful read, and only then the
real board (empty copy included). Like ``test_board_view``, these assert the page's
source contract (no JS runtime in the suite).
"""

from __future__ import annotations

from project_board.board_view import BOARD_PAGE


def _fn(name: str) -> str:
    """The body of a top-level ``function <name>(…)`` in the page's JS."""
    start = BOARD_PAGE.index(f"function {name}(")
    end = BOARD_PAGE.index("\n}\n", start)
    return BOARD_PAGE[start:end]


def test_skeleton_render_path_exists_for_kanban_and_list():
    body = _fn("renderSkeleton")
    assert "ensureLanes();" in body  # the Kanban lanes, built once as before
    assert "skelCardHTML(" in body  # skeleton cards in each lane
    assert '"skel-row"' in body or "skel-row" in body  # and skeleton rows in the list
    assert '$("rows").innerHTML = rows;' in body


def test_skeleton_uses_the_design_system_skeleton_classes():
    """The shimmer (and its reduced-motion static fill) is the DS kit's `.pl-skel`, not a
    local copy — the page must not define its own skeleton animation."""
    card = _fn("skelCardHTML")
    assert 'class="pl-skel-lines"' in card
    assert card.count('class="pl-skel"') == 2  # a title line + a meta line
    assert 'class="pl-skel"' in _fn("renderSkeleton")  # the list rows too
    assert "pl-skel-shimmer" not in BOARD_PAGE  # no hand-rolled shimmer keyframes
    assert ".pl-skel{" not in BOARD_PAGE and ".pl-skel {" not in BOARD_PAGE
    # the card-shaped box is a plain .skel-card, never a .card the handlers would pick up
    assert 'class="skel-card"' in card and 'class="card' not in card


def test_skeleton_is_gated_on_the_first_load_only():
    render = _fn("render")
    # the gate comes before either view paints
    assert "if (!LOADED){ renderSkeleton(); return; }" in render
    assert render.index("if (!LOADED)") < render.index('if (VIEW === "kanban")')
    # LOADED flips once, on the first SUCCESSFUL read, and the skeletons are dropped then
    load = _fn("load")
    assert "if (!LOADED){ LOADED = true; LOAD_FAILED = false; clearSkeleton(); }" in load
    assert load.index("LOADED = true") < load.index("catch (e)")
    # nothing ever sets it back — a later poll can't flash skeletons
    assert BOARD_PAGE.count("LOADED = false") == 1  # the declaration only
    assert "let LOADED = false, LOAD_FAILED = false;" in BOARD_PAGE
    # skeletons paint at once, not after the handshake / the 800 ms fallback
    tail = BOARD_PAGE[BOARD_PAGE.index("kit.initPluginView(boot);") - 200 :]
    assert "\nrender();\nkit.initPluginView(boot);" in tail


def test_no_empty_text_and_neutral_counts_before_first_load():
    body = _fn("renderSkeleton")
    assert "lane.empty.hidden = true;" in body
    assert 'lane.count.textContent = "–";' in body
    assert "LANE_EMPTY" not in body
    assert "pl-empty" not in body


def test_loading_lanes_are_aria_busy_and_cleared_after_load():
    body = _fn("renderSkeleton")
    assert 'lane.el.setAttribute("aria-busy", busy ? "true" : "false");' in body
    assert '$("list").setAttribute("aria-busy", busy ? "true" : "false");' in body
    clear = _fn("clearSkeleton")
    assert 'lane.el.removeAttribute("aria-busy");' in clear
    assert '$("list").removeAttribute("aria-busy");' in clear
    assert '.querySelectorAll(".skel-card").forEach(n => n.remove());' in clear


def test_a_failed_first_load_stops_the_skeleton_but_never_shows_empty_text():
    """The error path still renders the callout; before any data loaded it also stops the
    shimmer (no skeleton forever) without falling through to the "Nothing…" lanes."""
    load = _fn("load")
    catch = load[load.index("catch (e) {") :]
    assert "if (!LOADED && !LOAD_FAILED){ LOAD_FAILED = true; render(); }" in catch
    assert "Could not load the board: " in catch  # the existing error path is intact
    body = _fn("renderSkeleton")
    assert "const busy = !LOAD_FAILED;" in body
    assert "const n = busy ? SKEL_CARDS - (li % 2) : 0;" in body
