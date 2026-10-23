"""The card drawer: every coding card opens it, with Card and Code tabs.

Structural checks on the assembled page (the seams), plus the diff renderer run under node
against a /changes-shaped payload: escaping, line tints, a redacted file that never shows
content, and the "No local worktree" state.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from project_board.board_view import BOARD_PAGE

node = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


def _fn(name: str) -> str:
    start = BOARD_PAGE.index(f"function {name}(")
    nxt = BOARD_PAGE.find("\nfunction ", start + 1)
    nxt_async = BOARD_PAGE.find("\nasync function ", start + 1)
    ends = [e for e in (nxt, nxt_async) if e != -1]
    return BOARD_PAGE[start : min(ends)]


def test_every_coding_card_and_row_carries_a_data_card_handle():
    assert 'setData(n, "card", isTask(f) ? null : f.id);' in BOARD_PAGE
    assert "' data-card=\"'+esc(f.id)+'\"'" in BOARD_PAGE
    assert 'const cel = e.target.closest("[data-card]");' in BOARD_PAGE
    assert 'if (cel) { openCard(cel.getAttribute("data-card")); return; }' in BOARD_PAGE
    # Enter / Space on a focused card opens it too
    assert 'n.hasAttribute("data-mon") || n.hasAttribute("data-task") || n.hasAttribute("data-card")' in BOARD_PAGE
    # a card's PR link opens the PR, not the drawer
    assert 'if (e.target.closest("a[href]")) return;' in BOARD_PAGE


def test_the_drawer_has_an_accessible_tablist():
    assert 'id="drawer-tabs" role="tablist" aria-label="Card views" hidden' in BOARD_PAGE
    tabs = _fn("renderTabs")
    assert 'role="tab"' in tabs and 'aria-selected="' in tabs and 'aria-controls="drawer-body"' in tabs
    assert 'body.setAttribute("role", "tabpanel");' in tabs
    assert 'body.setAttribute("aria-labelledby", "dtab-"+cur);' in tabs
    # roving tabindex + arrow keys / Home / End
    assert "tabindex=\"'+(t === cur ? 0 : -1)+'\"" in tabs
    assert "{ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1}" in BOARD_PAGE


def test_code_tab_shows_for_building_cards_or_once_a_run_or_worktree_is_found():
    has = _fn("hasCode")
    assert 'f.state === "in_progress" || CODE_SEEN[fid]' in has
    probe = _fn("probeCode")
    assert '"/progress"' in probe and '"/changes"' in probe
    assert "CODE_SEEN[fid] = true; renderTabs();" in probe
    # default tab: Code while building, Card otherwise; a remembered tab wins
    assert 'tab || CARD_TAB[fid] || (f && f.state === "in_progress" ? "code" : "card")' in BOARD_PAGE


def test_the_code_tab_is_the_monitor_plus_changes_and_only_it_polls_changes():
    mon = _fn("openMonitor")
    assert 'enterCard(fid, "code");' in mon and "startChanges(fid);" in mon
    assert 'body.insertAdjacentHTML("beforeend", changesSection());' in BOARD_PAGE
    poll = _fn("pollChanges")
    assert "if (document.hidden || MON_FID !== fid) return;" in poll  # Code tab visible only
    assert "if (MON_FID !== fid || seq !== CHG_SEQ) return;" in poll  # a late answer is dropped
    assert "const CHG_POLL_MS = 10000;" in BOARD_PAGE
    # leaving the Code tab, opening a task, or closing stops the Changes poll
    assert "stopChanges();" in _fn("openCard")
    assert "stopChanges();" in _fn("leaveCard")
    assert "leaveCard();" in _fn("openTask")
    assert "leaveCard(true);" in _fn("closeMonitor")


def test_the_card_tab_reads_the_single_card_route_fenced_like_the_task_drawer():
    fetch = _fn("fetchCardDetail")
    assert "api(FEAT + encodeURIComponent(fid))" in fetch
    assert fetch.count("if (CARD_FID !== fid || seq !== CARD_DETAIL_SEQ[fid]) return;") == 2
    sync = _fn("syncCardDrawer")
    assert "if (!CARD_FID) return;" in sync
    assert "if (!force && html === CARD_LAST) return;" in sync  # an unchanged poll touches nothing
    assert "syncCardDrawer();\n}" in BOARD_PAGE  # render()'s tail keeps it in step


_RENDER = r"""
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeHref = (u) => /^https?:/.test(String(u)) ? String(u) : "#";
let CARD_FID = "bd-x", CHG_DATA = null; const CHG_UI = {};
const FEATS = {"bd-x": {id: "bd-x", pr_url: "https://github.com/o/r/pull/9"}};
const featureById = (id) => FEATS[id];
__FNS__
const data = JSON.parse(process.argv[1]);
CHG_DATA = {fid: "bd-x", data};
for (const k of data.open || []) CHG_UI[k] = true;
process.stdout.write(changesSection());
"""


def _render_changes(payload: dict) -> str:
    fns = "\n".join(
        BOARD_PAGE[BOARD_PAGE.index(m) : BOARD_PAGE.index(e)]
        for m, e in [
            ("const DIFF_STATUS =", "function changeFile("),
            ("function changeFile(", "// The Changes section"),
            (
                "function changesSection(",
                '$("drawer-body").addEventListener("click", (e) => {\n  const t = e.target.closest("[data-chg-toggle]")',
            ),
        ]
    )
    js = _RENDER.replace("__FNS__", fns)
    out = subprocess.run(["node", "-e", js, json.dumps(payload)], capture_output=True, text=True, check=True)
    return out.stdout


@node
def test_changes_render_escapes_tints_and_keeps_redacted_content_out():
    tree = {
        "path": "/w",
        "branch": "feat/bd-x",
        "base": "main",
        "file_count": 2,
        "additions": 2,
        "deletions": 1,
        "files": [
            {
                "path": "src/<b>.py",
                "status": "M",
                "additions": 2,
                "deletions": 1,
                "diff": "@@ -1 +1,2 @@\n-old <script>\n+new\n+more\n",
            },
            {"path": ".env", "status": "A", "redacted": True, "redacted_reason": "an environment file", "diff": ""},
        ],
    }
    html = _render_changes({"worktrees": [tree], "open": ["/w:src/<b>.py", "/w:.env"]})
    assert "src/&lt;b&gt;.py" in html and "<b>" not in html  # paths escaped
    assert "-old &lt;script&gt;" in html and "<script>" not in html  # diff text escaped
    assert '<span class="dl dl-h">@@ -1 +1,2 @@</span>' in html
    assert '<span class="dl dl-d">' in html and html.count('<span class="dl dl-a">') == 2
    assert "Content hidden: an environment file." in html
    assert "feat/bd-x vs main · 2 files" in html


@node
def test_changes_render_points_at_the_pr_when_no_worktree_is_left():
    html = _render_changes({"worktrees": []})
    assert "No local worktree" in html
    assert 'href="https://github.com/o/r/pull/9"' in html
