"""card-from-chat: ``board_watch`` / ``board_projects``, the Board view's follow mode, and
the ``card-from-chat`` skill.

The demo flow: the user asks for a feature in chat, the agent answers with an inline form
(an ADR 0118 interactive answer), the user sends it back, the agent creates and readies the
card, and ``board_watch`` opens the Board on the main stage following that card lane by lane.

All host-free. The registry is the vendored testkit's ``FakeRegistry`` (which namespaces
``emit`` the way the real bus does and records ``navigate``); the store is a stub.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import project_board as pb
from project_board.board_view import BOARD_PAGE

from _plugin_testkit import FakeRegistry  # protoAgent's own testkit, vendored verbatim (tests/ is on sys.path)

ROOT = Path(__file__).resolve().parent.parent
node = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def get_feature(self, fid):
        return self.rows.get(fid)


class _OrderedRegistry(FakeRegistry):
    """Records emit and navigate in ONE list, so the publish-then-navigate order is assertable."""

    def __init__(self, **kw):
        super().__init__(plugin_id="project_board", **kw)
        self.calls: list = []

    def emit(self, topic, data=None):
        super().emit(topic, data)
        self.calls.append(("emit", self.emitted[-1][0]))

    def navigate(self, view=""):
        super().navigate(view)
        self.calls.append(("navigate", view))


def _tools(monkeypatch, registry, rows=None, cfg=None):
    store = _Store(rows if rows is not None else {"bd-a1b2": {"id": "bd-a1b2", "board_state": "ready"}})
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: store)
    return {t.name: t for t in pb._board_tools(cfg or {}, registry=registry)}


# ── board_watch ──────────────────────────────────────────────────────────────────


def test_board_watch_publishes_focus_then_navigates_to_the_board(monkeypatch):
    reg = _OrderedRegistry()
    reply = _tools(monkeypatch, reg)["board_watch"].invoke({"feature_id": "bd-a1b2"})
    assert reg.emitted == [("project_board.focus", {"id": "bd-a1b2"})]
    assert reg.navigations == ["board"]
    # publish FIRST: a view the navigate mounts is caught up by the host's replay of it
    assert reg.calls == [("emit", "project_board.focus"), ("navigate", "board")]
    assert reply.startswith("Following bd-a1b2 on the Board (on deck)")
    assert "\n" not in reply  # one short confirmation line


def test_board_watch_targets_a_view_the_manifest_declares():
    views = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())["views"]
    board = next(v for v in views if v["id"] == pb.BOARD_VIEW)
    # no `placement` (or rail) = the console's main stage, not a right/bottom dock
    assert board.get("placement", "rail") == "rail"


def test_board_watch_strips_quotes_and_accepts_dotted_ids(monkeypatch):
    reg = _OrderedRegistry()
    rows = {"bd-vbyd.24": {"id": "bd-vbyd.24", "board_state": "backlog"}}
    _tools(monkeypatch, reg, rows)["board_watch"].invoke({"feature_id": '"bd-vbyd.24"'})
    assert reg.emitted == [("project_board.focus", {"id": "bd-vbyd.24"})]


@pytest.mark.parametrize("bad", ["", "nope", "bd-a1/../x", "bd-a1\n", "<script>"])
def test_board_watch_refuses_a_non_id_and_drives_nothing(monkeypatch, bad):
    reg = _OrderedRegistry()
    reply = _tools(monkeypatch, reg)["board_watch"].invoke({"feature_id": bad})
    assert reply.startswith("Error:")
    assert reg.calls == []


def test_board_watch_refuses_an_unknown_card(monkeypatch):
    reg = _OrderedRegistry()
    reply = _tools(monkeypatch, reg, rows={})["board_watch"].invoke({"feature_id": "bd-zzzz"})
    assert reply == "Error: no board card 'bd-zzzz'."
    assert reg.calls == []


def test_board_watch_without_a_registry_says_so(monkeypatch):
    reply = _tools(monkeypatch, None)["board_watch"].invoke({"feature_id": "bd-a1b2"})
    assert reply.startswith("Error: this host can't drive the console")


def test_register_wires_board_watch_to_the_real_registry(monkeypatch):
    """register() hands its registry to the tools, so the registered board_watch drives it."""
    store = _Store({"bd-a1b2": {"id": "bd-a1b2", "board_state": "in_progress"}})
    monkeypatch.setattr("project_board.store.get_store", lambda **_kw: store)
    reg = FakeRegistry(plugin_id="project_board")
    pb.register(reg)
    watch = next(t for t in reg.tools if t.name == "board_watch")
    assert watch.invoke({"feature_id": "bd-a1b2"}).startswith("Following bd-a1b2")
    assert reg.emitted[-1] == ("project_board.focus", {"id": "bd-a1b2"})
    assert reg.navigations == ["board"]


# ── board_projects ───────────────────────────────────────────────────────────────


def test_board_projects_lists_the_configured_map_with_its_default(monkeypatch, tmp_path):
    cfg = {
        "projects": {"web": {"repo": str(tmp_path)}, "api": {"repo": str(tmp_path), "base_branch": "dev"}},
        "default_project": "web",
    }
    out = json.loads(_tools(monkeypatch, None, cfg=cfg)["board_projects"].invoke({}))
    assert [p["name"] for p in out["projects"]] == ["api", "web"]
    assert out["projects"][0]["base_branch"] == "dev"
    assert out["default_project"] == "web"


def test_board_projects_names_the_implicit_project_when_the_registry_is_empty(monkeypatch, tmp_path):
    """A flat single-repo board (`repo:`, no `projects:` map) on a host whose project
    registry answers with NOTHING: the registry snapshot succeeds but is empty. The board
    still has one project, the implicit `default` every card is stamped with. It used to
    come back empty, so the card-from-chat form had no project and the agent warned the
    user that creation might fail."""
    import importlib

    reg = importlib.import_module(f"{pb.__name__}.project_registry")
    monkeypatch.setattr(reg, "project_registry_snapshot", lambda: {"projects": [], "default_project": ""})
    out = json.loads(_tools(monkeypatch, None, cfg={"repo": str(tmp_path)})["board_projects"].invoke({}))
    assert [p["name"] for p in out["projects"]] == ["default"]
    assert out["projects"][0]["repo"] == str(tmp_path)
    assert out["default_project"] == "default"


# ── the Board view's follow mode ─────────────────────────────────────────────────


def _js(start: str, end: str) -> str:
    return BOARD_PAGE[BOARD_PAGE.index(start) : BOARD_PAGE.index(end)]


def test_view_subscribes_to_the_focus_topic_board_watch_publishes():
    assert f'const FOCUS_TOPIC = "project_board.{pb.FOCUS_EVENT}";' in BOARD_PAGE
    assert 'postMessage({type: "protoagent:subscribe", patterns: [FOCUS_TOPIC]}, "*")' in BOARD_PAGE
    listener = _js('window.addEventListener("message"', 'try { window.parent.postMessage({type: "protoagent:subscribe"')
    assert 'm.type !== "protoagent:event"' in listener
    assert "e.source !== window.parent" in listener  # only the host may start a follow
    assert "if (m.topic === FOCUS_TOPIC) startFollow(m.data && m.data.id);" in listener


def test_followed_card_carries_the_ring_and_the_tag_across_renders():
    # in the markup (cardHTML) and the class (updateCard), so every re-render keeps both
    assert "(isFollowed(f.id) ? '<span class=\"follow-tag\">Following</span>' : '')" in BOARD_PAGE
    assert '(isFollowed(f.id) ? " card--following" : "")' in BOARD_PAGE
    # every Kanban paint re-checks the followed card
    rk = _js("function renderKanban()", "function flip(")
    assert rk.rstrip().endswith("followAfterRender();\n}") or "followAfterRender();" in rk


def test_follow_scrolls_after_each_lane_move_and_ends_at_done_or_on_the_operator():
    body = _js("function followAfterRender()", "// The operator takes over")
    assert "if (f.state === FOLLOW.state) return;" in body  # only a lane move scrolls
    assert "MOVE_MS + STAGGER_MAX * STAGGER_MS" in body  # after the FLIP has played
    assert 'if (f.state === "done")' in body and "LAND_MS + 400" in body  # the glow plays first
    assert 'behavior: motionOK() ? "smooth" : "auto"' in body  # reduced motion: no smooth scroll
    takeover = _js("// The operator takes over", "// The event bus across the sandbox")
    for ev in ('"pointerdown"', '"wheel"', '"touchstart"', '"keydown"'):
        assert ev in takeover
    assert "e.isTrusted" in takeover


def test_follow_css_uses_tokens_and_respects_reduced_motion():
    css = _js("/* Follow mode (board_watch)", "@media (prefers-reduced-motion:reduce){\n    .lane[")
    assert "var(--pl-color-accent)" in css
    assert "#" not in css and "rgb(" not in css and "rgba(" not in css, "tokens only, no hard-coded colours"
    assert "infinite" not in css  # nothing loops on camera
    rm = BOARD_PAGE[BOARD_PAGE.index("@media (prefers-reduced-motion:reduce){\n    .lane[") :]
    assert ".card--follow-in{animation:none}" in rm[: rm.index("}\n  }")]


@node
def test_follow_id_guard_matches_the_tool_id_shape():
    """The view's id guard accepts what board_watch accepts and nothing else."""
    from project_board.board_cards import _ID_RE

    line = next(ln for ln in BOARD_PAGE.splitlines() if ln.startswith("const FOLLOW_ID_RE = "))
    cases = ["bd-a1b2", "bd-vbyd.24", "x-1", "", "nope", "bd-a1/x", "<b>-1", "bd-" + "a" * 70]
    js = line + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(c => FOLLOW_ID_RE.test(c))));"
    got = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert got == [bool(_ID_RE.fullmatch(c)) for c in cases]


# ── the card-from-chat skill ─────────────────────────────────────────────────────


def _skill():
    text = (ROOT / "skills" / "card-from-chat" / "SKILL.md").read_text()
    return yaml.safe_load(text.split("---", 2)[1]), text


def test_card_from_chat_skill_is_discovered_in_the_registered_skill_dir():
    """register() adds `skills/`; the host discovers one SKILL.md per sub-directory, keyed by
    its frontmatter `name`, which must match the directory."""
    found = {p.parent.name: p for p in (ROOT / "skills").glob("*/SKILL.md")}
    assert "card-from-chat" in found
    fm, _ = _skill()
    assert fm["name"] == "card-from-chat"
    assert fm["description"] and "board" in fm["description"]


def test_card_from_chat_skill_names_real_tools():
    fm, _ = _skill()
    names = {t.name for t in pb._board_tools({})}
    board_tools = [t for t in fm["tools"] if t.startswith("board_")]
    assert set(board_tools) <= names
    for t in ("board_projects", "board_create_feature", "board_mark_ready", "board_watch", "show_artifact"):
        assert t in fm["tools"]


def test_card_from_chat_skill_uses_the_adr_0118_send_bridge_not_hitl():
    fm, text = _skill()
    assert "window.protoArtifact.send(" in text
    assert 'typeof window.protoArtifact?.send !== "function"' in text  # feature-detected
    assert "request_user_input" not in fm["tools"]
    assert "not** the" in text and "HITL" in text
    # the button sends every value, in the shape step 2 reads back
    for field in ("Create board card:", "Project:", "Title:", "Priority:", "What and why:", "Acceptance criteria:"):
        assert field in text
    # create → ready (the loop only claims ready cards) → watch
    flow = text[text.index("## 2. When the values arrive") :]
    assert flow.index("board_create_feature") < flow.index("board_mark_ready") < flow.index("board_watch")


def test_card_form_skeleton_never_submits_a_form():
    """The inline artifact frame is sandboxed without allow-forms: a <form> submit is blocked
    and its handler never runs, so the confirm chip never appears. The skeleton must click."""
    skill = (Path(pb.__file__).parent / "skills" / "card-from-chat" / "SKILL.md").read_text(encoding="utf-8")
    assert "<form" not in skill.split("```html", 1)[1].split("```", 1)[0]
    assert 'type="submit"' not in skill and 'addEventListener("submit"' not in skill
    assert '$("go").addEventListener("click"' in skill
